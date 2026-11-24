"""JA-09 pilot: validate is offline, run is free/test, run-live requires explicit paid authorization."""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
from uuid import uuid4
from .contracts import Attempt
from .suite import load_suite
from .grading import grade,summarize

def write(path,value):path.write_bytes((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())

def provenance(root):
    def git(*args):
        try:
            r=subprocess.run(['git',*args],cwd=root,capture_output=True,check=False)
            return r.stdout if r.returncode==0 else None
        except OSError:return None
    head=git('rev-parse','HEAD');patch=git('diff','HEAD','--','backend','benchmarks','pyproject.toml','uv.lock')
    paths=[*sorted((root/'backend/src').rglob('*.py')),root/'pyproject.toml',root/'uv.lock']
    source={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}
    return {'git_commit':head.decode().strip() if head else None,'workspace_patch_sha256':hashlib.sha256(patch).hexdigest() if patch is not None else None,
            'source_sha256':source,'python':sys.version,'no_model_private_thoughts_recorded':True}

async def ledger(owner):
    from psycopg import AsyncConnection
    async with await AsyncConnection.connect(owner.conninfo()) as c:
        return await (await c.execute('SELECT id::text,authorization_id,state,reserved_nano,accounted_nano FROM app.model_http_calls ORDER BY id')).fetchall()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['validate','run','run-live','authorize','review-export','review-validate','output-review-export','output-review-validate'])
    parser.add_argument('--root',type=Path,default=Path.cwd());parser.add_argument('--suite',type=Path)
    parser.add_argument('--split',choices=['development','reserved_candidate'],default='development')
    parser.add_argument('--include-reserved',action='store_true');parser.add_argument('--repeats',type=int,choices=[1,2,3],default=1)
    parser.add_argument('--adapters',nargs='+',choices=['system_scripted','fixed_rules','system_live','fixed_prefetch_live'])
    parser.add_argument('--tasks',nargs='+');parser.add_argument('--worker-config',type=Path);parser.add_argument('--owner-config',type=Path)
    parser.add_argument('--policy',type=Path);parser.add_argument('--authorization');parser.add_argument('--credentials',type=Path)
    parser.add_argument('--max-cny');parser.add_argument('--review-file',type=Path)
    parser.add_argument('--run-dir',type=Path);parser.add_argument('--bundle',type=Path)
    parser.add_argument('--intent-findings',type=Path);parser.add_argument('--intent-source-run',type=Path)
    args=parser.parse_args(argv);root=args.root.resolve()
    directory=args.suite or root/('benchmarks/career-pilot-v0.1' if args.command in ('validate','run') else 'benchmarks/career-pilot-v0.2')
    manifest,tasks,oracles=load_suite(directory)
    if args.command in ('output-review-export','output-review-validate'):
        from .output_reviews import export_outputs,validate_outputs
        if not args.run_dir:parser.error('output review requires --run-dir; saved results only, no model calls')
        if args.command=='output-review-export':
            out=root/'.private/ja-09-03/output-reviews'/uuid4().hex
            result=export_outputs(out,args.run_dir,manifest,tasks,oracles);result['output']=str(out)
        else:
            if not args.bundle:parser.error('output-review-validate requires --bundle')
            result=validate_outputs(args.bundle,args.run_dir,manifest,tasks,oracles)
        print(json.dumps(result,ensure_ascii=False));return 0
    if args.command=='validate':
        print(json.dumps({'status':'valid','suite':manifest['version'],'tasks':len(tasks),'splits':manifest['splits'],
            'human_review':'pending','reserved_execution':False,'model_http_calls':0},ensure_ascii=False));return 0
    if args.split=='reserved_candidate' and not args.include_reserved:parser.error('reserved_candidate requires --include-reserved; record access and do not tune on these outcomes')
    selected=[t for t in tasks if t.split==args.split]
    if args.tasks:
        if len(args.tasks)!=len(set(args.tasks)) or not set(args.tasks)<={t.id for t in selected}:parser.error('task selection must be unique and within this split')
        selected=[t for t in selected if t.id in args.tasks]
    if args.command in ('review-export','review-validate'):
        from .reviews import export_reviews,validate_reviews
        if args.command=='review-export':
            out=root/'.private/ja-09-02/reviews'/uuid4().hex
            result=export_reviews(out,manifest,selected,oracles);result.update(output=str(out),split=args.split)
        else:
            if not args.review_file:parser.error('review-validate requires --review-file')
            result=validate_reviews(args.review_file,manifest,tasks,oracles,selected_ids=[t.id for t in selected])
        print(json.dumps(result,ensure_ascii=False));return 0
    from interview_agent.storage.config import DatabaseSettings
    from .adapters import execute_input,arun,billing_summary,LIVE_ADAPTERS
    if args.command=='authorize':
        from decimal import Decimal,InvalidOperation
        from interview_agent.billing.ledger import authorize
        from .billing import policy_for_stage
        if not args.owner_config or not args.policy or not args.authorization or args.max_cny is None:parser.error('authorize requires this-stage ID, policy, owner config and explicit --max-cny (no automatic authorization)')
        owner=DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding='utf-8'));policy,_=policy_for_stage(args.policy,args.authorization)
        if owner.dbname!='py_interview':parser.error('live authorization uses development DB only')
        try:
            value=Decimal(args.max_cny);nano=value*Decimal(1000000000)
            if not value.is_finite() or not 0<value<=2 or nano!=nano.to_integral_value():raise ValueError()
        except (ValueError,InvalidOperation):parser.error('authorization must be a positive exact amount <= CNY2')
        arun(authorize(owner,policy,args.authorization,int(nano)))
        print(json.dumps({'authorization_id':args.authorization,'max_nano':int(nano),'model_http_calls':0}));return 0
    live=args.command=='run-live'
    adapters=args.adapters or (['system_live','fixed_prefetch_live'] if live else ['system_scripted','fixed_rules'])
    if len(adapters)!=len(set(adapters)) or any((a in LIVE_ADAPTERS)!=live for a in adapters):parser.error('adapters must match free run or explicit run-live')
    if not args.worker_config or not args.owner_config:parser.error('run requires explicit worker/owner configs; no credentials in tasks')
    from interview_agent.worker.config import WorkerSettings
    settings=WorkerSettings.load(args.worker_config);owner=DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding='utf-8'))
    options=None;auth_before=None;review_overlay=None;intent_summary=None;live_stage=None
    if args.review_file:
        from .reviews import validate_reviews
        review_overlay=validate_reviews(args.review_file,manifest,tasks,oracles,selected_ids=[t.id for t in selected])
    if live:
        from .billing import policy_for_stage,check_authorization,CALIBRATION_AUTHORIZATION_ID,calibration_scope
        if not args.tasks or not args.policy or not args.credentials or not args.authorization:parser.error('run-live requires explicit task IDs, policy, credentials and this-stage authorization; it never authorizes automatically')
        if settings.mode!='development' or settings.database.dbname!='py_interview' or owner.dbname!='py_interview':parser.error('live runs require explicit development configuration; traces/fees will be retained')
        if any(t.input.conditions.auto_research for t in selected):parser.error('this live comparison does not authorize automatic research')
        policy,live_stage=policy_for_stage(args.policy,args.authorization)
        if args.authorization==CALIBRATION_AUTHORIZATION_ID:
            calibration_scope([t.id for t in selected],adapters,args.repeats,args.split)
            if not args.intent_findings or not args.intent_source_run:parser.error('calibration requires --intent-findings and original --intent-source-run')
            from .calibration import validate_findings,validate_live_intent_scope
            from .domain_suite import load_domain_suite
            from .domain_contracts import DomainAttempt
            dm,dt,do=load_domain_suite(root/'benchmarks/job-domain-v0.1')
            validate_findings(args.intent_findings,args.intent_source_run,dm,dt,do,attempt_contract=DomainAttempt)
            intent_summary=validate_live_intent_scope(args.intent_findings,manifest,tasks,oracles)
        arun(check_authorization(owner,policy,args.authorization))
        options={'policy':policy,'authorization':args.authorization,'credentials':args.credentials}
        auth_before=arun(billing_summary(settings,options))
        if auth_before['usage_status']=='unknown':parser.error('unknown usage requires operator reconciliation; no automatic retry')
    elif settings.mode!='test' or settings.database.dbname!='py_interview_test' or owner.dbname!='py_interview_test':parser.error('free run uses test environment only')
    stage=live_stage if live else 'ja-09-02' if manifest['version']!='career-pilot-v0.1' else 'ja-09-01'
    out=root/('.private/'+stage+'/benchmark')/uuid4().hex;out.mkdir(parents=True)
    metadata={'suite':manifest,'split':args.split,'selected_ids':[t.id for t in selected],'adapters':adapters,'repeats':args.repeats,
        'label_access':'producers receive PublicInput only; operator-authored reserved candidates are NOT a secret/independent holdout',
        'reserved_executed':args.split=='reserved_candidate','provenance':provenance(root),'operator_task_review':review_overlay,
        'scope':'real ASGI/API and PostgreSQL/Redis; setup is scripted; no network browser',
        'scoped_intent_findings':intent_summary,
        'same_model_baseline':'shared policy/SYSTEM_PROMPT/tools/SQL gates/max3 HTTP; deterministic prefetch differs' if live else 'fixed_rules is not an LLM',
        'authorization_before':auth_before,'policy':options['policy'].model_dump(mode='json') if live else None,
        'retained_live_accounts':live,'output_semantic_human_review':'pending'}
    write(out/'run.json',metadata);before=arun(ledger(owner));records=[];stopped=None
    with (out/'attempts.jsonl').open('x',encoding='utf-8',newline='\n') as handle:
        for task in selected:
            for repeat in range(1,args.repeats+1):
                order=adapters if (int(task.id[1:])+repeat)%2 else list(reversed(adapters))
                for adapter in order:
                    if stopped:result={'status':'not_run','error_code':stopped}
                    else:result=execute_input(task.input,adapter,settings,owner,**({'live_options':options} if live else {}))
                    attempt=Attempt(task_id=task.id,adapter=adapter,repeat=repeat,**result)
                    record={'attempt':attempt.model_dump(mode='json'),'grade':grade(attempt,oracles[task.id]),'human_rubric':oracles[task.id].human_rubric}
                    records.append(record);handle.write(json.dumps(record,ensure_ascii=False)+'\n');handle.flush()
                    if live and attempt.status in ('authorization_stopped','infrastructure_failure'):stopped=attempt.error_code or 'live_run_stopped'
    report=summarize(records,[t.id for t in selected],adapters,args.repeats)
    after=arun(ledger(owner))
    if live:
        after_map={r[0]:r for r in after}
        old_unchanged=all(after_map.get(r[0])==r for r in before if r[1]!=args.authorization)
        report.update(old_authorizations_unchanged=old_unchanged,authorization_final=arun(billing_summary(settings,options)))
    else:old_unchanged=before==after;report['model_ledger_unchanged']=old_unchanged
    report.update(suite=manifest['version'],split=args.split,reserved_executed=args.split=='reserved_candidate',human_review='pending',
        same_model_baseline='prefetch_exploratory' if live else 'not_run',stop_reason=stopped,operator_task_review=review_overlay)
    write(out/'report.json',report)
    infra=sum(v['infrastructure_failures'] for v in report['metrics'].values());complete=not infra and old_unchanged and not stopped
    print(json.dumps({'status':'recorded' if complete else 'incomplete','output':str(out),'metrics':report['metrics'],'domain_success_rate':None},ensure_ascii=False))
    return 0 if complete else 1

if __name__=='__main__':raise SystemExit(main())
