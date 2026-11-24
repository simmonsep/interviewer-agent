"""Bounded core journey; SDK mock by default, live needs explicit stage approval."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
from uuid import uuid4
import httpx
from fastapi.testclient import TestClient
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.app import create_app
from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import Settings,DatabaseSettings
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport
from interview_agent.contracts.operations import QueueMessage
from interview_agent.billing.ledger import authorize
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for as career_worker
from interview_agent.career.coordinator import ScriptedCoordinator
from interview_agent.career.billing import CareerPolicy,CareerLedger
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.worker import worker_for as material_worker
from interview_agent.materials.agent import ScriptedMaterials
from interview_agent.materials.billing import MaterialPolicy,MaterialLedger
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for as interview_worker
from interview_agent.interviews.agent import ScriptedInterviewer
from interview_agent.interviews.billing import InterviewPolicy,InterviewLedger

ROOT=Path(__file__).resolve().parents[1]
LIMITS={'career':500_000_000,'materials':500_000_000,'interviews':1_000_000_000}
PHASE='core-06-03-20261031-cny2'
CLOSEOUT_STAGE='closeout-20261119'
STAGES={'core-06-03':(PHASE,'core0603'),'core-06-05':('core-06-05-20261101-cny2','core0605'),
        'core-06-07':('core-06-07-20261101-cny15','core0607'),
        'core-06-09':('core-06-09-20261102-cny15','core0609'),
        CLOSEOUT_STAGE:('closeout-20261119-cny2','core0603'),
        # 新增业务 1: a fresh authorization id for the RAG/adoption run. Caps stay at 2 CNY and
        # the stage is separate on purpose - the closeout stage is single-use by design.
        'rag-20261119':('rag-20261119-cny2','core0603'),
        # Second live run: the bank-selected adoption test. Separate id because the first one is
        # single-use, and caps stay at 2 CNY.
        'rag2-20261119':('rag2-20261119-cny2','core0603'),
        # Third live run: a JD that mismatches the bank, to see whether the interviewer still
        # reaches for unrelated entries (skip behaviour cannot be measured with a matching JD).
        'rag3-20261119':('rag3-20261119-cny2','core0603'),
        # Fourth live run: unrelated JD + the imported bank, to observe the bank_use declaration.
        'rag4-20261119':('rag4-20261119-cny2','core0603'),
        # Fifth live run: does the interviewer actually cite a frozen project strategy?
        'rag5-20261119':('rag5-20261119-cny2','core0603'),
        # Sixth live run: does the model adopt the server-suggested strategy?
        'rag6-20261119':('rag6-20261119-cny2','core0603'),
        # Seventh live run: does a JD-shaped suggested_topics order change the first question?
        'rag7-20261119':('rag7-20261119-cny2','core0603'),
        # Eighth live run: a JD that matches topics the imported bank actually contains.
        'rag8-20261119':('rag8-20261119-cny2','core0603'),
        # Ninth live run: does the prompt's preference for suggested_topics change the choice?
        'rag9-20261119':('rag9-20261119-cny2','core0603'),
        # Tenth live run: §2.1 private preference enforced by the server (same bank + JD as rag8/9).
        'rag10-20261119':('rag10-20261119-cny2','core0603'),
        # rag10 exhausted its rounds on a bare 'invalid_interview_tool'; retry after teaching the repair.
        'rag11-20261119':('rag11-20261119-cny2','core0603'),
        # F07Z-160: the selection rules now travel inside the frozen preinstalled skill.
        'rag12-20261119':('rag12-20261119-cny2','core0603'),
        # Node C acceptance: project question first, then a private-bank technical question.
        'rag13-20261119':('rag13-20261119-cny2','core0603'),
        # Node C: project question allowed AND a matching private bank in the same session.
        'rag14-20261119':('rag14-20261119-cny2','core0603'),
        # Node C: a lexical false hit - the JD wants locks/idempotency, the bank only has cache-hit rate.
        'rag15-20261119':('rag15-20261119-cny2','core0603'),
        # rag15 stopped before materials (material_proposal_required_or_pause); retry the fallback sample.
        'rag16-20261119':('rag16-20261119-cny2','core0603'),
        # rag16 obeyed a lexical false hit; skill v2 tells the model to fall back with a reason.
        'rag17-20261119':('rag17-20261119-cny2','core0603'),
        # rag18: the tightened gate should drop the false hit, so the role pack answers the locks JD.
        'rag18-20261119':('rag18-20261119-cny2','core0603'),
        # Fallback sample: a candidate whose title matches the JD but whose question is shallow.
        'rag19-20261119':('rag19-20261119-cny2','core0603')}
JD='Java后端实习：SQL事务、接口幂等、Redis并发控制；需要说明个人贡献与验证方法。'
SOURCE='我亲自实现了SQL事务中的幂等请求记录。\n我用独立数据库验证重复请求与提交后响应丢失的情况。'


def write(p,v):p.write_bytes((json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n').encode())


def stage_plan(stage):
    if stage not in STAGES:raise ValueError('unsupported_stage')
    remaining=stage in ('core-06-07','core-06-09')
    return {'limits':{k:v for k,v in LIMITS.items() if not remaining or k!='materials'},
            'max_cny':'1.5' if remaining else '2','scripted_preparation':remaining}


def validate_training_result(s):
    questions=[e for e in s['events'] if e['kind']=='question']
    answers=[e for e in s['events'] if e['kind']=='answer']
    if s['status']!='COMPLETED' or len(questions)!=2 or len(answers)!=2:raise RuntimeError('two_question_training_required')
    if s['policy'].get('coverage',{}).get('version')!='project-technical-v1' or sum(q['payload']['topic_id']=='project' for q in questions)!=1:
        raise RuntimeError('project_and_technical_coverage_required')
    observations=s['observations']
    if len(observations)!=2 or {o['answer_id'] for o in observations}!={a['id'] for a in answers}:raise RuntimeError('each_answer_observation_required')
    if any(not o.get('evidence_quote') or not o.get('answer_quote') or o.get('assisted') for o in observations) or any(e['kind']=='explanation' for e in s['events']):
        raise RuntimeError('unassisted_source_bound_feedback_required')


def validate_application_review(goal,application,session,original):
    action=goal.get('action') or {};plan=action.get('application_plan') or {}
    refs=plan.get('evidence_refs',[]);observations={o['id'] for o in session['observations']}
    if (goal['status']!='WAITING_FOR_USER' or action.get('type')!='prepare_application_action'
        or goal.get('application_plan_state')!='current' or plan.get('execution')!='proposal_only'
        or plan.get('application_id')!=application['id'] or 'session:'+session['id'] not in refs
        or not any('observation:'+oid in refs for oid in observations)):
        raise RuntimeError('current_source_bound_application_proposal_required')
    if any(application[k]!=original[k] for k in ('status','version','package','current_package_revision','submitted_package_revision')):
        raise RuntimeError('application_progress_or_package_changed')


def validate_summary_result(session,application=None):
    if session['policy'].get('feedback_summary',{}).get('version')!='project-extractive-v1':
        raise RuntimeError('project_summary_policy_required')
    answers={e['id']:e['payload']['text'] for e in session['events'] if e['kind']=='answer'}
    projects=[o for o in session['observations'] if o['topic_id']=='project']
    if not projects:raise RuntimeError('project_observation_required')
    for o in projects:
        original=answers.get(o['answer_id'],'');excerpt=original[:120]+('…' if len(original)>120 else '')
        if (not original or o.get('summary_origin')!='server_answer_excerpt'
            or not o['summary'].startswith('个人经历未核验。原回答节选：「'+excerpt+'」。')):
            raise RuntimeError('project_answer_excerpt_required')
    report=session.get('report') or {}
    if report.get('summary_origin')!='server_session_counts' or '个人经历未核验' not in (report.get('model_summary') or ''):
        raise RuntimeError('server_count_summary_required')
    if application is not None:
        hypotheses=application['review']['training_hypotheses']
        for o in projects:
            linked=next((h for h in hypotheses if 'observation:'+o['id'] in h.get('evidence_refs',[])),None)
            if not linked or linked.get('summary_origin')!=o['summary_origin'] or linked['summary']!=o['summary']:
                raise RuntimeError('application_summary_provenance_required')


def preflight(args):
    plan=stage_plan(args.stage)
    live=args.mode=='live'
    if live and args.mock_unknown:raise ValueError('live_must_not_use_mock_faults')
    if live and (not args.execute_live or args.max_cny!=plan['max_cny'] or not args.credentials):raise ValueError('live_requires_explicit_stage_approval_flags')
    if not live and args.credentials:raise ValueError('mock_must_not_read_real_credentials')
    api=Settings.load(args.api_config);worker=WorkerSettings.load(args.worker_config)
    owner=DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding='utf-8'))
    # The RAG run reuses the test database on purpose: the dev database is not part of this
    # machine's reproducible setup, and the trial already proved the test DB path end to end.
    test_db_stage = args.stage in (CLOSEOUT_STAGE, 'rag-20261119', 'rag2-20261119',
                                   'rag3-20261119', 'rag4-20261119', 'rag5-20261119',
                                   'rag6-20261119', 'rag7-20261119', 'rag8-20261119', 'rag9-20261119', 'rag10-20261119', 'rag11-20261119', 'rag12-20261119', 'rag13-20261119', 'rag14-20261119', 'rag15-20261119', 'rag16-20261119', 'rag17-20261119', 'rag18-20261119', 'rag19-20261119')
    expected='development' if live and not test_db_stage else 'test'
    if api.mode!=expected or worker.mode!=expected or owner.user!='py_interview_owner':raise ValueError('phase_environment_mismatch')
    if any(getattr(api.database,k)!=getattr(worker.database,k) or getattr(api.database,k)!=getattr(owner,k) for k in ('host','port','dbname')):raise ValueError('database_profiles_must_match')
    if owner.dbname!=('py_interview' if expected=='development' else 'py_interview_test'):raise ValueError('phase_database_mismatch')
    return api,worker,owner


async def run(args):
    api,settings,owner=preflight(args);live=args.mode=='live';nonce=uuid4().hex
    phase,profile=STAGES[args.stage]
    plan=stage_plan(args.stage);limits=plan['limits'];scripted_preparation=plan['scripted_preparation']
    out=(args.output or ROOT/'.private'/profile)/nonce;out.mkdir(parents=True)
    credentials=args.credentials
    if not live:
        credentials=out/'mock-credentials.env';credentials.write_bytes(b'DEEPSEEK_API_KEY=offline-not-real\n')
    prefix=phase if live else 'mock-'+phase+'-'+nonce
    identities={k:prefix+'-'+k for k in limits}
    types={'career':(CareerPolicy,CareerLedger,CareerRepository,career_worker,ScriptedCoordinator,'career_steps'),
           'materials':(MaterialPolicy,MaterialLedger,MaterialRepository,material_worker,ScriptedMaterials,'career_material_runs'),
           'interviews':(InterviewPolicy,InterviewLedger,InterviewRepository,interview_worker,ScriptedInterviewer,'career_interview_runs')}
    policies={k:types[k][0].load(ROOT/f'configs/{profile}-{k}.json') for k in limits}
    receipt={'status':'needs_review','mode':args.mode,'phase':phase,'max_cny':plan['max_cny'],'role_limits_nano':limits,
             'max_http_admissions':sum(p.max_http_attempts for p in policies.values()),'human_review':'pending','domain_success_rate':None,'checks':[],
             'scripted_preparation_and_first_question':scripted_preparation,
             'planned_paid_tasks':3 if scripted_preparation else 6,
             'provider_http':0 if not live else None,'synthetic_confirmation':True,
             'transport':'real SDK MockTransport' if not live else 'real SDK provider transport',
             'local_api':'ASGI authentication/API, not a network-browser test','accounts_retained':live}
    frozen=[*sorted((ROOT/'backend/src').rglob('*.py')),ROOT/'scripts/check-live-core.py',ROOT/'uv.lock',
            *[ROOT/f'configs/{profile}-{k}.json' for k in policies]]
    write(out/'plan.json',{'jd':JD,'source':SOURCE,'policies':{k:v.model_dump() for k,v in policies.items()},'authorizations':identities,
                          'stage_plan':plan,'max_http_admissions':receipt['max_http_admissions'],'planned_paid_tasks':receipt['planned_paid_tasks'],
                          'source_file_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen}})
    uid=None;streams=[];stage='authorization';pool_ready=False
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row});redis=settings.redis.connect()
    async def bills():return {k:await types[k][1](pool,policies[k],identities[k]).summary() for k in policies}
    async def pump(kind,gid,*,scripted=False):
        policy_type,ledger_type,repo_type,factory,predictor_type,table=types[kind];repo=repo_type(pool)
        async with pool.connection() as conn,conn.transaction():
            row=await (await conn.execute(f"SELECT * FROM app.{table} WHERE goal_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",(gid,))).fetchone()
            if not row:raise RuntimeError('expected_pending_job_'+kind)
            goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(gid,))).fetchone()
            scope=await repo.context_at(conn,goal) if kind=='career' else row['input_payload']
        async def noop():pass
        predictor=None if live else predictor_type(scope,noop)
        async def mock(request):
            body=json.loads(request.content);messages=[SimpleNamespace(text=m.get('content') or '') for m in body['messages']]
            response=await predictor.complete(messages,[])
            calls=[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments,ensure_ascii=False)}} for c in response.tool_calls]
            return httpx.Response(200,json={'id':'mock-'+uuid4().hex,'object':'chat.completion','created':1,'model':'DeepSeek-V4.1-Flash-1017',
                'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':response.text,'tool_calls':calls}}],
                'usage':None if args.mock_unknown else {'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100}})
        stream=f'py-interview:{settings.mode}:{nonce}:jobs:v1:{kind}';streams.append(stream) if stream not in streams else None
        q=QueueTransport(redis,stream);await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
        if scripted:kw={'mode' if kind=='career' else 'provider':'scripted'}
        else:
            kw={'policy':policies[kind],'authorization':identities[kind],'credentials':credentials}
            kw['mode' if kind=='career' else 'provider']='live'
            if not live:kw['inner_factory']=lambda:httpx.MockTransport(mock)
        await factory(repo,q,**kw).once()
        summaries=await bills();write(out/'fees.json',summaries)
        if any(s['usage_status']=='unknown' for s in summaries.values()):raise RuntimeError('unknown_usage_paused_no_retry')
    def check(value,code):
        if not value:raise RuntimeError(code)
        receipt['checks'].append(code)
    try:
        await pool.open(wait=True);pool_ready=True;await redis.ping()
        async with await AsyncConnection.connect(owner.conninfo()) as c:
            rows=await (await c.execute('SELECT id FROM app.model_authorizations WHERE id=ANY(%s)',(list(identities.values()),))).fetchall()
            if rows:raise RuntimeError('stage_already_initialized_do_not_retry')
        for k in policies:await authorize(owner,policies[k],identities[k],limits[k])
        with TestClient(create_app(api),base_url=api.origin,backend_options={'loop_factory':selector_loop},raise_server_exceptions=False) as client:
            client.headers['Origin']=api.origin
            def request(path,body=None):
                r=client.get(path) if body is None else client.post(path,json=body,headers={'Idempotency-Key':uuid4().hex})
                if r.status_code>=400:raise RuntimeError('HTTP_'+str(r.status_code)+'_'+r.json().get('code','unknown'))
                return r.json()
            uid=request('/api/auth/sign-up/email',{'email':prefix+'@example.com','name':'Synthetic','password':secrets.token_urlsafe(24)})['user']['id']
            stage='intent_materials'
            jd_text=args.jd or JD
            direction='AI 应用' if args.role=='ai-application' else 'Java'
            g=request('/api/career-goals/',{'message':'我有这份实习JD，想先梳理项目经历再练面试，暂时不要搜其它岗位。','jd_text':jd_text,'conditions':{'direction':direction}})
            gid=g['id'];base='/api/career-goals/'+gid;receipt.update(user_id=uid,goal_id=gid)
            source=request(base+'/materials/sources',{'expected_version':1,'expected_revision':0,'name':'synthetic.md','format':'markdown','text':SOURCE})
            await pump('career',gid,scripted=scripted_preparation);v=request(base);write(out/'01-coordinator.json',v)
            check(v['action'] and v['action']['type']=='prepare_materials','material_proposal_required_or_pause')
            if args.stage=='core-06-05':
                check('已上传1份材料' in v['action']['text'] and '已有0条用户确认事实' in v['action']['text']
                      and '无需重复上传已有材料' in v['action']['text'],'uploaded_material_state_consistent')
            stage='material_analysis'
            r=request(base+f"/steps/{v['steps'][-1]['id']}/confirm-materials",{'expected_version':1,'expected_revision':1})
            rid=r['run_id']
            await pump('materials',gid,scripted=scripted_preparation);m=request(base+'/materials');write(out/'02-materials.json',m)
            run=next(item for item in m['runs'] if item['id']==rid)
            check(run['status']=='SUCCEEDED' and run['result'].get('action')=='analysis' and run['result'].get('proposals'),'material_analysis_required_or_pause')
            request(base+'/materials/confirmations',{'expected_version':1,'expected_revision':1,'run_id':run['id'],'proposal_ids':list(range(min(2,len(run['result']['proposals']))))})
            a=request(base+'/applications',{'expected_version':1,'expected_material_revision':2,'company':'合成公司','title':'Java实习','source_id':source['source_id'],'application_url':'https://example.com/apply'})
            ap=base+'/applications/'+a['id'];a=request(ap+'/progress',{'expected_application_version':a['version'],'status':'SUBMITTED','note':'合成操作声明，未外部投递','evidence_kind':'user_confirmation'})
            stage='interview'
            banks=[]
            if args.bank_doc:
                import json as _json
                document=_json.loads(args.bank_doc.read_text(encoding='utf-8'))
                preview=request('/api/question-banks/imports',{'document':document})
                commit=request(f"/api/question-banks/imports/{preview['preview_id']}/commit",
                               {'bank_id':None,'expected_version':0})
                banks=[{'bank_id':commit['bank_id'],'version':commit['version']}]
                write(out/'00-bank.json',{'document_name':document.get('name'),
                                          'items':len(document.get('items') or []),**commit})
            body={'expected_version':1,'role':args.role,'application_id':a['id'],
                  'interview_round':'合成技术面','max_questions':2}
            if banks: body['question_banks']=banks
            if args.allow_project_questions: body['allow_project_questions']=True
            s=request(base+'/interviews',body)
            if scripted_preparation:
                check(len(s['material_fact_ids'])==2 and s['policy'].get('coverage',{}).get('version')=='project-technical-v1','confirmed_facts_and_coverage_frozen_before_model')
            if args.stage=='core-06-09':
                check(s['policy'].get('feedback_summary',{}).get('version')=='project-extractive-v1','project_summary_policy_frozen_before_model')
            path=base+'/interviews/'+s['id']
            for turn in range(3):
                try:await pump('interviews',gid,scripted=scripted_preparation and turn==0)
                finally:
                    s=request(path);write(out/f'03-interview-{turn}.json',s)
                if turn==2:break
                check(s['status']=='WAITING_FOR_USER','interview_question_required_or_pause')
                q=next(e for e in reversed(s['events']) if e['kind']=='question');topic=q['payload']['topic_id']
                answer={'project':'我亲自实现SQL事务中的请求记录，用唯一约束和事务提交验证重复请求；不确定的边界仍需测试。',
                        'redis-lock':'只用SETNX就够了，释放时直接DEL。','sql-isolation':'加volatile就能让数据库事务串行执行。',
                        'http-idempotency':'每次重试都再创建一笔订单，不保存请求身份。'}.get(topic,'我不清楚这个主题，暂时只能描述一个待验证的猜想。')
                request(path+'/answers',{'expected_version':s['version'],'question_id':q['id'],'text':answer})
            check(s['status']=='COMPLETED','two_question_interview_completed')
            if scripted_preparation or args.stage==CLOSEOUT_STAGE:validate_training_result(s)
            if args.stage in ('core-06-09',CLOSEOUT_STAGE):validate_summary_result(s)
            if args.stage=='core-06-05':
                questions=[e['payload'] for e in s['events'] if e['kind']=='question']
                check(s['policy'].get('coverage',{}).get('version')=='project-technical-v1'
                      and len(questions)==2 and sum(q['topic_id']=='project' for q in questions)==1,'project_and_technical_coverage')
            stage='application_review'
            request(base+'/messages',{'expected_version':1,'message':'请复盘这份申请的面试记录，给出有依据的下一步，不能推断被拒原因。','intent':'review'})
            await pump('career',gid);v=request(base);write(out/'04-application-plan.json',v)
            check(v['status']=='WAITING_FOR_USER' and v['action']['type'] in ('prepare_application_action','ask_user'),'review_proposal_or_clarification')
            final_application=request(ap);write(out/'05-application.json',final_application)
            if scripted_preparation or args.stage==CLOSEOUT_STAGE:validate_application_review(v,final_application,s,a)
            if args.stage in ('core-06-09',CLOSEOUT_STAGE):validate_summary_result(s,final_application)
            receipt['status']='protocol_passed_semantics_pending'
    except Exception as error:receipt.update(failed_stage=stage,error_type=type(error).__name__,error_code=str(error) if isinstance(error,RuntimeError) else None)
    finally:
        if pool_ready:
            try:receipt['fees']=await bills()
            except Exception:receipt['fee_summary_unavailable']=True
        try:
            if streams:await redis.delete(*streams)
        except Exception:receipt['queue_cleanup_failed']=True
        await redis.aclose();await pool.close()
        rubrics=(['项目/技术覆盖与选题针对性（首题scripted）','回答反馈/引用及模拟面试提示政策','复盘引用与不推断拒绝原因']
                 if scripted_preparation else ['意图理解与岗位范围','材料引用/事实不虚构','项目追问与基础问题针对性','错误回答反馈及模拟面试提示政策','复盘引用与不推断拒绝原因'])
        review={'status':'pending','mode':args.mode,'human_reviewer':None,'rubrics':[
            {'topic':x,'decision':None,'notes':''} for x in rubrics]}
        review['plan_sha256']=hashlib.sha256((out/'plan.json').read_bytes()).hexdigest()
        review['output_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob('0*.json'))}
        write(out/'human-review.json',review)
        if not live:
            async with await AsyncConnection.connect(owner.conninfo(),autocommit=True) as c:
                if uid:await c.execute('DELETE FROM auth.users WHERE id=%s',(uid,))
                await c.execute('DELETE FROM app.model_http_calls WHERE authorization_id=ANY(%s)',(list(identities.values()),))
                await c.execute('DELETE FROM app.model_authorizations WHERE id=ANY(%s)',(list(identities.values()),))
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'mode':args.mode,'output':str(out),'failed_stage':receipt.get('failed_stage')}))
    return 0 if receipt['status']=='protocol_passed_semantics_pending' else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--mode',choices=['sdk-mock','live'],default='sdk-mock')
    p.add_argument('--stage',choices=list(STAGES),default='core-06-03',help='Separate authorization IDs/policies; old default is retained and cannot reuse initialized live IDs')
    for name in ['api-config','worker-config','owner-config']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--credentials',type=Path);p.add_argument('--execute-live',action='store_true');p.add_argument('--max-cny')
    p.add_argument('--mock-unknown',action='store_true',help='SDK mock only: inject missing usage to verify whole-journey pause')
    p.add_argument('--role',choices=['java-backend','ai-application'],default='java-backend',
                   help='role of the bank-doc interview; the bank items must declare the same role')
    p.add_argument('--jd',default='',help='optional JD text for the bank-doc run')
    p.add_argument('--bank-doc',type=Path,
                   help='optional question-bank JSON (product format); the session opens on it')
    p.add_argument('--allow-project-questions',action='store_true',
                   help='with --bank-doc: also allow project questions (default is off for bank sessions)')
    p.add_argument('--output',type=Path);args=p.parse_args()
    raise SystemExit(asyncio.run(run(args),loop_factory=selector_loop))
