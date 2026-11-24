"""CORE-06-06/11/23: one bounded interview decision; SDK mock by default, no rerun."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
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
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.worker import worker_for as material_worker
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for as interview_worker
from interview_agent.interviews.agent import ScriptedInterviewer
from interview_agent.interviews.billing import InterviewPolicy,InterviewLedger

ROOT=Path(__file__).resolve().parents[1]
PHASE='core-06-06-20261101-cny05'
STAGES={'core-06-06':(PHASE,'core0606'),'core-06-11':('core-06-11-20261103-cny05','core0611')}
LIMIT=500_000_000
BANK_STAGE='core-06-23'
BANK_PHASE='core-06-23-20261109-cny06'
STAGES[BANK_STAGE]=(BANK_PHASE,'core0623')
BANK_LIMIT=600_000_000
BANK_FIXTURE=ROOT/'fixtures/question-banks/agent-developer-trial.json'
BANK_ANSWER='我会用 checkpointer 按 thread_id 保存会话状态。同一会话重启后沿用原 ID，并使用数据库保存检查点。'

JD='Java后端实习：SQL事务、接口幂等、Redis并发控制；需要说明个人贡献与验证方法。'
SOURCE='我亲自实现了SQL事务中的幂等请求记录。\n我用独立数据库验证重复请求与提交后响应丢失的情况。'


def write(path,value):path.write_bytes((json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n').encode())


def preflight(args):
    if getattr(args,'stage','core-06-06') not in STAGES:raise ValueError('unsupported_stage')
    if getattr(args,'stage','core-06-06')=='core-06-11' and args.mock_fault=='reject-once':raise ValueError('unsupported_mock_fault_for_stage')
    live=args.mode=='live'
    approved_cny='0.6' if getattr(args,'stage','core-06-06')==BANK_STAGE else '0.5'
    if live and args.mock_fault!='none':raise ValueError('live_must_not_use_mock_faults')
    if live and (not args.execute_live or args.max_cny!=approved_cny or not args.credentials):raise ValueError('live_requires_explicit_stage_approval_flags')
    if not live and args.credentials:raise ValueError('mock_must_not_read_real_credentials')
    api=Settings.load(args.api_config);worker=WorkerSettings.load(args.worker_config)
    owner=DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding='utf-8'))
    expected='development' if live else 'test'
    if api.mode!=expected or worker.mode!=expected or owner.user!='py_interview_owner':raise ValueError('phase_environment_mismatch')
    if any(getattr(api.database,k)!=getattr(worker.database,k) or getattr(api.database,k)!=getattr(owner,k) for k in ('host','port','dbname')):raise ValueError('database_profiles_must_match')
    if owner.dbname!=('py_interview' if live else 'py_interview_test'):raise ValueError('phase_database_mismatch')
    return api,worker,owner


async def mock_completion(request, scripted, fault, *, bank_followup):
    # Run the actual SDK serialization/translation and original billing transport.
    response=await scripted.complete([],[])
    if bank_followup and scripted.n in (3,4):
        # Use the actual tool response serialized by the SDK, including on repair.
        tool_messages=[m for m in json.loads(request.content)['messages'] if m['role']=='tool']
        latest=json.loads(tool_messages[-1]['content'])
        arguments=response.tool_calls[0].arguments
        next_topic=arguments['topic_id']
        if scripted.n==4:
            if latest.get('error')!='bank_question_text_is_frozen':
                raise RuntimeError('expected_bank_rejection_before_repair')
            topics=latest['bank_submission_options']
            identity_field='topic_id'
        else:
            topics=latest['topics']
            identity_field='id'
        selected=next(t for t in topics if t[identity_field]==next_topic)
        arguments.update(selected['questions'][0]['submission'])
    if fault=='reject-once' and scripted.n==3:
        response.tool_calls[0].arguments['question_text']='SYNTHETIC-PRIVATE-WORDING'
    calls=[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments,ensure_ascii=False)}} for c in response.tool_calls]
    return httpx.Response(200,json={'id':'mock-'+uuid4().hex,'object':'chat.completion','created':1,'model':'deepseek-flash',
        'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':'','tool_calls':calls}}],
        'usage':None if fault=='unknown' else {'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100}})


async def run(args):
    api,settings,owner=preflight(args);live=args.mode=='live';nonce=uuid4().hex
    phase,profile=STAGES[getattr(args,'stage','core-06-06')];after_answer=profile=='core0611'
    bank_followup=profile=='core0623'
    limit=BANK_LIMIT if bank_followup else LIMIT
    max_cny='0.6' if bank_followup else '0.5'
    policy=InterviewPolicy.load(ROOT/f'configs/{profile}-interviews.json')
    out=(args.output or ROOT/'.private'/profile)/nonce;out.mkdir(parents=True)
    identity=phase if live else 'mock-'+phase+'-'+nonce
    credentials=args.credentials
    if not live:
        credentials=out/'mock-credentials.env';credentials.write_bytes(b'DEEPSEEK_API_KEY=offline-not-real\n')
    frozen=[*sorted((ROOT/'backend/src').rglob('*.py')),Path(__file__).resolve(),ROOT/f'configs/{profile}-interviews.json',ROOT/'uv.lock']
    if bank_followup:
        frozen.append(BANK_FIXTURE)
    plan_input={'bank':BANK_FIXTURE.relative_to(ROOT).as_posix(),'synthetic_answer':BANK_ANSWER} if bank_followup else {'jd':JD,'source':SOURCE}
    write(out/'plan.json',{'phase':phase,'mode':args.mode,**plan_input,'authorization':identity,'after_project_answer':after_answer,
        'max_nano':limit,'max_http_admissions':4,'paid_tasks':1,'preparation':'scripted only; new synthetic user/facts, no old run reuse',
        'policy':policy.model_dump(),'mock_fault':args.mock_fault,'source_file_sha256':{
            p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen}})
    receipt={'status':'needs_review','mode':args.mode,'phase':phase,'max_cny':max_cny,'max_http_admissions':4,
        'domain_success_rate':None,'human_review':'pending','accounts_retained':live,
        'local_api':'authenticated ASGI, not network/browser','preparation_provider':'scripted_protocol',
        'provider_http':None if live else 0,'scope':'project-answer feedback and technical next question only' if after_answer else 'first question only; no answers, finish or application review'}
    if bank_followup:
        receipt['scope']='synthetic bank answer observation and frozen second question only'
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect();uid=None;streams=[];ready=False;stage='authorization'
    async def pump(kind,gid,*,scripted_only=False):
        classes={'career':(CareerRepository,career_worker,'career_steps'),
                 'materials':(MaterialRepository,material_worker,'career_material_runs'),
                 'interviews':(InterviewRepository,interview_worker,'career_interview_runs')}
        cls,factory,table=classes[kind];repo=cls(pool)
        async with pool.connection() as c:
            row=await (await c.execute(f"SELECT * FROM app.{table} WHERE goal_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",(gid,))).fetchone()
        if not row:raise RuntimeError('expected_pending_'+kind)
        stream=f'py-interview:{settings.mode}:{nonce}:jobs:v1:{kind}';streams.append(stream)
        q=QueueTransport(redis,stream)
        await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
        kw={'mode' if kind=='career' else 'provider':'scripted'}
        if kind=='interviews' and not scripted_only:
            async def noop():pass
            scripted=ScriptedInterviewer(row['input_payload'],noop)
            async def mock(request):
                return await mock_completion(request,scripted,args.mock_fault,bank_followup=bank_followup)
            kw={'provider':'live','policy':policy,'authorization':identity,'credentials':credentials}
            if not live:kw['inner_factory']=lambda:httpx.MockTransport(mock)
        if not await factory(repo,q,**kw).once():raise RuntimeError('worker_did_not_take_owned_job')
    try:
        await pool.open(wait=True);ready=True;await redis.ping()
        async with await AsyncConnection.connect(owner.conninfo()) as c:
            if await (await c.execute('SELECT id FROM app.model_authorizations WHERE id=%s',(identity,))).fetchone():raise RuntimeError('stage_already_initialized_do_not_retry')
        await authorize(owner,policy,identity,limit)
        with TestClient(create_app(api),base_url=api.origin,backend_options={'loop_factory':selector_loop},raise_server_exceptions=False) as client:
            client.headers['Origin']=api.origin
            def request(path,body=None):
                r=client.get(path) if body is None else client.post(path,json=body,headers={'Idempotency-Key':uuid4().hex})
                if r.status_code>=400:raise RuntimeError('HTTP_'+str(r.status_code)+'_'+r.json().get('code','unknown'))
                return r.json()
            stage='scripted_preparation'
            uid=request('/api/auth/sign-up/email',{'email':identity+'@example.com','name':'Synthetic','password':secrets.token_urlsafe(24)})['user']['id']
            if bank_followup:
                g=request('/api/career-goals/',{'message':'进行岗位面试训练','intent':'training',
                    'conditions':{'direction':'Python Agent 开发（LangGraph、状态与工具调用）'}})
                gid=g['id']
                base='/api/career-goals/'+gid
                receipt.update(user_id=uid,goal_id=gid)
                document=json.loads(BANK_FIXTURE.read_text(encoding='utf-8'))
                preview=request('/api/question-banks/imports',{'document':document})
                bank=request('/api/question-banks/imports/'+preview['preview_id']+'/commit',{})
                s=request(base+'/interviews',{'expected_version':1,'role':'ai-application','mode':'interview',
                    'max_questions':2,'duration_seconds':3600,'question_banks':[bank],'allow_project_questions':False})
                if s['material_fact_ids'] or len(s['question_banks'])!=1:
                    raise RuntimeError('expected_material_free_frozen_bank')
            else:
                g=request('/api/career-goals/',{'message':'先梳理项目材料再练Java面试，不研究其它岗位。','jd_text':JD,'conditions':{'direction':'Java'}})
                gid=g['id'];base='/api/career-goals/'+gid;receipt.update(user_id=uid,goal_id=gid)
                await pump('career',gid)
                request(base+'/materials/sources',{'expected_version':1,'expected_revision':0,'name':'synthetic.md','format':'markdown','text':SOURCE})
                m=request(base+'/materials/runs',{'expected_version':1,'expected_revision':1,'mode':'analyze'})
                await pump('materials',gid);materials=request(base+'/materials');write(out/'01-scripted-materials.json',materials)
                r=next(r for r in materials['runs'] if r['id']==m['run_id'])
                if r['status']!='SUCCEEDED' or len(r['result'].get('proposals',[]))!=2:raise RuntimeError('scripted_preparation_failed')
                request(base+'/materials/confirmations',{'expected_version':1,'expected_revision':1,'run_id':r['id'],'proposal_ids':[0,1]})
                facts=request(base+'/materials')['facts']
                if len(facts)!=2:raise RuntimeError('two_confirmed_synthetic_facts_required')
                s=request(base+'/interviews',{'expected_version':1,'role':'java-backend','max_questions':2,'fact_ids':[f['id'] for f in facts]})
                if s['policy'].get('coverage',{}).get('version')!='project-technical-v1' or len(s['material_fact_ids'])!=2:
                    raise RuntimeError('mixed_coverage_policy_required_before_model')
            path=base+'/interviews/'+s['id'];receipt['session_id']=s['id'];stage='first_question'
            if bank_followup:
                expected={item['item_id']:item['question'] for item in document['items']}
                await pump('interviews',gid,scripted_only=True)
                s=request(path)
                write(out/'02-scripted-first-question.json',s)
                first=next(e for e in s['events'] if e['kind']=='question')
                if (s['status']!='WAITING_FOR_USER' or first['payload']['bank_item']['item_id']!='thread-memory'
                    or first['payload']['text']!=expected['thread-memory']):
                    raise RuntimeError('scripted_frozen_first_question_required')
                request(path+'/answers',{'expected_version':s['version'],'question_id':first['id'],'text':BANK_ANSWER})
                stage='after_bank_answer'
                try:
                    await pump('interviews',gid)
                finally:
                    s=request(path)
                    write(out/'03-after-answer.json',s)
                fees=await InterviewLedger(pool,policy,identity).summary()
                write(out/'fees.json',fees)
                if fees['usage_status']=='unknown':
                    raise RuntimeError('unknown_usage_paused_no_retry')
                questions=[event for event in s['events'] if event['kind']=='question']
                answers=[event for event in s['events'] if event['kind']=='answer']
                if s['status']!='WAITING_FOR_USER' or len(questions)!=2 or len(answers)!=1:
                    raise RuntimeError('bank_next_question_required_or_pause')
                next_question=questions[-1]['payload']
                if (next_question['bank_item']['item_id']!='interrupt-side-effects'
                    or next_question['text']!=expected['interrupt-side-effects'] or s['observations']):
                    raise RuntimeError('frozen_next_question_and_hidden_feedback_required')
                async with pool.connection() as conn:
                    rows=await (await conn.execute(
                        'SELECT id,answer_id,payload FROM app.career_interview_observations WHERE session_id=%s',(s['id'],))).fetchall()
                write(out/'04-bank-observation.json',rows)
                if len(rows)!=1 or str(rows[0]['answer_id'])!=answers[0]['id']:
                    raise RuntimeError('bank_observation_required')
                quote=rows[0]['payload'].get('answer_quote')
                if not quote or quote not in BANK_ANSWER:
                    raise RuntimeError('bank_observation_quote_required')
                receipt['status']='bank_followup_protocol_passed_semantics_pending'
            elif after_answer:
                if s['policy'].get('feedback_summary',{}).get('version')!='project-extractive-v1':raise RuntimeError('summary_policy_required_before_model')
                await pump('interviews',gid,scripted_only=True);s=request(path);write(out/'02-scripted-first-question.json',s)
                first=next(e for e in s['events'] if e['kind']=='question')
                if s['status']!='WAITING_FOR_USER' or first['payload']['kind']!='project':raise RuntimeError('scripted_project_first_required')
                original='我亲自实现SQL事务中的请求记录，用唯一约束和事务提交验证重复请求；不确定的边界仍需测试。'
                request(path+'/answers',{'expected_version':s['version'],'question_id':first['id'],'text':original})
                stage='after_project_answer'
                try:await pump('interviews',gid)
                finally:
                    s=request(path);write(out/'03-after-answer.json',s)
                fees=await InterviewLedger(pool,policy,identity).summary();write(out/'fees.json',fees)
                if fees['usage_status']=='unknown':raise RuntimeError('unknown_usage_paused_no_retry')
                questions=[e for e in s['events'] if e['kind']=='question'];answers=[e for e in s['events'] if e['kind']=='answer']
                if s['status']!='WAITING_FOR_USER' or len(questions)!=2 or len(answers)!=1 or questions[-1]['payload']['topic_id']=='project':raise RuntimeError('technical_next_question_required_or_pause')
                # Test controller inspects its own synthetic session; public interview feedback remains hidden.
                async with pool.connection() as conn:
                    rows=await (await conn.execute('SELECT id,answer_id,payload FROM app.career_interview_observations WHERE session_id=%s',(s['id'],))).fetchall()
                write(out/'04-project-observation.json',rows)
                if len(rows)!=1 or str(rows[0]['answer_id'])!=answers[0]['id']:raise RuntimeError('project_observation_required')
                o=rows[0]['payload']
                if (o.get('summary_origin')!='server_answer_excerpt' or not o['summary'].startswith('个人经历未核验。原回答节选：「'+original+'」。')
                    or o['answer_quote'] not in original or o['evidence_id']!=first['payload']['fact_id']):raise RuntimeError('grounded_project_feedback_required')
                receipt['status']='after_answer_protocol_passed_semantics_pending'
            else:
                await pump('interviews',gid);s=request(path);write(out/'02-first-question.json',s)
                fees=await InterviewLedger(pool,policy,identity).summary();write(out/'fees.json',fees)
                if fees['usage_status']=='unknown':raise RuntimeError('unknown_usage_paused_no_retry')
                questions=[e for e in s['events'] if e['kind']=='question']
                if s['status']!='WAITING_FOR_USER' or len(questions)!=1 or any(e['kind']=='answer' for e in s['events']):raise RuntimeError('first_question_required_or_pause')
                if s['policy'].get('coverage',{}).get('version')!='project-technical-v1':raise RuntimeError('mixed_coverage_policy_required')
                receipt['status']='first_question_protocol_passed_semantics_pending'
    except Exception as error:
        receipt.update(failed_stage=stage,error_type=type(error).__name__,error_code=str(error) if isinstance(error,RuntimeError) else None)
    finally:
        if ready:
            try:receipt['fees']=await InterviewLedger(pool,policy,identity).summary()
            except Exception:receipt['fee_summary_unavailable']=True
        try:
            if streams:await redis.delete(*streams)
        except Exception:receipt['queue_cleanup_failed']=True
        await redis.aclose();await pool.close()
        write(out/'human-review.json',{'status':'pending','reviewer':None,'domain_success_rate':None,
            'rubrics':['观察是否依据合成原答并指出缺失要点','第二题是否按冻结题库推进且未泄题'] if bank_followup else ['项目节选与标签/引用的适当性','技术第二题与JD和原回答的针对性'] if after_answer else ['首题与JD及个人声明是否相关','是否守住模拟面试无答案提示/不虚构事实'],
            'plan_sha256':hashlib.sha256((out/'plan.json').read_bytes()).hexdigest(),
            'output_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob('0*.json'))}})
        if not live:
            async with await AsyncConnection.connect(owner.conninfo(),autocommit=True) as c:
                if uid:await c.execute('DELETE FROM auth.users WHERE id=%s',(uid,))
                await c.execute('DELETE FROM app.model_http_calls WHERE authorization_id=%s',(identity,))
                await c.execute('DELETE FROM app.model_authorizations WHERE id=%s',(identity,))
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'mode':args.mode,'output':str(out),'failed_stage':receipt.get('failed_stage')}))
    return 0 if receipt['status'] in ('first_question_protocol_passed_semantics_pending','after_answer_protocol_passed_semantics_pending','bank_followup_protocol_passed_semantics_pending') else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['sdk-mock','live'],default='sdk-mock')
    p.add_argument('--stage',choices=list(STAGES),default='core-06-06')
    for name in ['api-config','worker-config','owner-config']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--credentials',type=Path);p.add_argument('--execute-live',action='store_true');p.add_argument('--max-cny')
    p.add_argument('--mock-fault',choices=['none','reject-once','unknown'],default='none');p.add_argument('--output',type=Path)
    raise SystemExit(asyncio.run(run(p.parse_args()),loop_factory=selector_loop))
