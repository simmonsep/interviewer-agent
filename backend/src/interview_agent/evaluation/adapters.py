"""Real test services with synthetic accounts; producers receive public input only."""
import asyncio,secrets,time
from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi.testclient import TestClient
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.app import create_app
from interview_agent.api.__main__ import selector_loop
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for as career_worker
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for as interview_worker
from interview_agent.contracts.operations import QueueMessage
from interview_agent.worker.queue import QueueTransport,TaskWorker
from interview_agent.career.billing import CareerLedger

LIVE_ADAPTERS={'system_live','fixed_prefetch_live'}

def arun(coro):return asyncio.run(coro,loop_factory=selector_loop)

@asynccontextmanager
async def resources(settings):
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,min_size=1,max_size=4,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect();stream='py-interview:test:'+uuid4().hex+':jobs:v1'
    try:
        await pool.open(wait=True,timeout=8);await redis.ping()
        yield pool,QueueTransport(redis,stream)
    finally:
        await redis.delete(stream);await redis.aclose();await pool.close()

async def fixed_rules(repo,claim):
    """Competent deterministic smoke baseline, NOT a same-model baseline."""
    c=await repo.context(claim);trace=[]
    async def step(name):
        await repo.admit_scripted(claim);trace.append({'tool':name,'status':'ok'})
    await step('read_goal');latest=c.messages[-1].text
    if any(word in latest for word in ('结束','先到这里','停止本轮')):
        d={'type':'finish','text':'按用户要求结束本轮。'}
    elif len(c.applications)>1:d={'type':'ask_user','text':'请确认要复盘的公司和岗位。'}
    elif c.applications:
        await step('read_application');v=await repo.application_context(claim,c.applications[0].id)
        action=next((a for a in v['actions'] if a['kind']=='retest_topic'),v['actions'][0])
        d={'type':'prepare_application_action','text':action['reason'],'application':{
            'application_id':v['application_id'],'context_sha256':v['context_sha256'],'action_id':action['id']}}
    elif c.jd_text.strip():d={'type':'prepare_training','text':'准备用户JD训练。'}
    elif c.conditions.direction:d={'type':'prepare_job_research','text':'按已确认方向准备研究。'}
    else:d={'type':'ask_user','text':'请确认技术方向与城市要求。'}
    await step(d['type']);await repo.save(claim,d,'scripted_protocol',trace)

async def pump(settings,gid,kind,adapter='system_scripted',sid=None,live_options=None):
    async with resources(settings) as (pool,q):
        if kind=='career':
            repo=CareerRepository(pool)
            if adapter=='fixed_rules':
                async def prepare(claim,lost):
                    if await repo.cached(claim) is None:await fixed_rules(repo,claim)
                worker=TaskWorker(repo,q,'eval-fixed-'+uuid4().hex,prepare=prepare)
            elif adapter in LIVE_ADAPTERS:
                worker=career_worker(repo,q,mode='live',coordinator_flow='fixed_prefetch' if adapter=='fixed_prefetch_live' else 'dynamic',**live_options)
            else:worker=career_worker(repo,q,mode='scripted')
            sql='SELECT id,job_id,goal_version FROM app.career_steps WHERE goal_id=%s ORDER BY goal_version DESC LIMIT 1';params=(gid,)
        else:
            repo=InterviewRepository(pool);worker=interview_worker(repo,q,provider='scripted')
            sql='SELECT id,job_id,goal_version FROM app.career_interview_runs WHERE goal_id=%s AND session_id=%s ORDER BY session_version DESC LIMIT 1';params=(gid,sid)
        async with pool.connection() as conn:row=await (await conn.execute(sql,params)).fetchone()
        if row is None:raise RuntimeError('owned_task_missing')
        # Notify only this owned episode; never dispatch all pending rows in a shared test DB.
        await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
        if not await worker.once():raise RuntimeError('owned_task_not_received')

async def cleanup(owner,uid):
    async with await AsyncConnection.connect(owner.conninfo()) as conn:
        await conn.execute('DELETE FROM auth.users WHERE id=%s',(uid,))

async def billing_summary(settings,options,gid=None):
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,min_size=1,max_size=4,kwargs={'row_factory':dict_row})
    try:
        await pool.open(wait=True,timeout=8)
        operation=None
        if gid:
            async with pool.connection() as conn:
                row=await (await conn.execute('SELECT id FROM app.career_steps WHERE goal_id=%s ORDER BY goal_version DESC LIMIT 1',(gid,))).fetchone()
                operation=str(row['id'])
        return await CareerLedger(pool,options['policy'],options['authorization']).summary(operation)
    finally:await pool.close()

def execute_input(public,adapter,settings,owner,*,live_options=None):
    """No task ID, oracle, rubric or expected action enters this producer interface."""
    live=adapter in LIVE_ADAPTERS
    if live:
        if not live_options or not all(live_options.get(k) for k in ('policy','authorization','credentials')):raise ValueError('evaluation_live_parameters_required')
        mock=live_options.get('inner_factory') is not None
        if not mock and (settings.mode!='development' or settings.database.dbname!='py_interview'):raise ValueError('evaluation_live_development_required')
        if public.conditions.auto_research:raise ValueError('evaluation_live_no_auto_research')
    elif settings.mode!='test' or settings.database.dbname!='py_interview_test' or owner.dbname!='py_interview_test':raise ValueError('evaluation_test_environment_required')
    if owner.dbname!=settings.database.dbname:raise ValueError('evaluation_database_mismatch')
    if (settings.database.host,settings.database.port)!=(owner.host,owner.port):raise ValueError('evaluation_database_mismatch')
    uid=None;gid=None;result=None;step='setup'
    try:
        with TestClient(create_app(settings),base_url=settings.origin,raise_server_exceptions=False,
                        backend_options={'loop_factory':selector_loop}) as c:
            c.headers['Origin']=settings.origin
            def get(path):
                r=c.get(path)
                if r.status_code>=400:raise RuntimeError('evaluation_get_failed')
                return r.json()
            def post(path,body):
                r=c.post(path,json=body,headers={'Idempotency-Key':uuid4().hex})
                if r.status_code>=400:raise RuntimeError('evaluation_fixture_command_failed')
                return r.json()
            u=post('/api/auth/sign-up/email',{'email':'eval-'+uuid4().hex+'@example.com','name':'Synthetic', 'password':secrets.token_urlsafe(24)})
            uid=u['user']['id']
            g=post('/api/career-goals/',{'message':public.message,'conditions':public.conditions.model_dump(),'jd_text':public.jd_text})
            gid=g['id'];base='/api/career-goals/'+gid;apps=[];retired=set();version=1
            setup=public.application
            if setup:
                a=post(base+'/applications',{'expected_version':1,'expected_material_revision':0,'company':'合成甲公司','title':'Java实习'})
                ap=base+'/applications/'+a['id'];apps.append(a['id'])
                if setup.status!='DRAFT':
                    a=post(ap+'/progress',{'expected_application_version':a['version'],'status':'SUBMITTED','note':setup.progress_note or '合成用户声明，未核验官网','evidence_kind':'user_confirmation'})
                    if setup.status!='SUBMITTED':
                        a=post(ap+'/progress',{'expected_application_version':a['version'],'status':setup.status,'note':'合成用户后续声明','evidence_kind':'user_confirmation'})
                if setup.closed:
                    a=post(ap+'/availability',{'expected_application_version':a['version'],'availability':'closed','note':'合成用户报告关闭，非独立核验'})
                if setup.training!='none':
                    s=post(base+'/interviews',{'expected_version':1,'role':'java-backend','mode':setup.training,'max_questions':2,'application_id':a['id'],'interview_round':'合成一面'})
                    path=base+'/interviews/'+s['id'];arun(pump(settings,gid,'interview',sid=s['id']));s=get(path)
                    for answer in ('不知道，只加volatile。','复合更新要原子性，并验证同步边界。'):
                        q=next(e for e in reversed(s['events']) if e['kind']=='question')
                        post(path+'/answers',{'expected_version':s['version'],'question_id':q['id'],'text':answer})
                        arun(pump(settings,gid,'interview',sid=s['id']));s=get(path)
                    if s['status']!='COMPLETED':raise RuntimeError('fixture_interview_incomplete')
                    retract=s['observations'] if setup.retract=='all' else s['observations'][:1] if setup.retract=='first' else []
                    for observation in retract:
                        post(base+'/interview-memory/'+observation['id']+'/retract',{'reason':'合成用户撤回，后续不要继续采用'})
                        retired.add('observation:'+observation['id'])
                if setup.multiple:
                    b=post(base+'/applications',{'expected_version':1,'expected_material_revision':0,'company':'合成乙公司','title':'Java实习'})
                    apps.append(b['id'])
                if setup.change_goal:
                    post(base+'/messages',{'expected_version':1,'message':public.message,'conditions':{'direction':'AI应用'},'jd_text':'Python新岗位，不覆盖历史Java包'})
                    version=2
            before={aid:get(base+'/applications/'+aid) for aid in apps};before_goal=get(base)
            setup_steps=before_goal['model_requests'];step='decision';started=time.perf_counter()
            arun(pump(settings,gid,'career',adapter,live_options=live_options));elapsed=(time.perf_counter()-started)*1000
            after=get(base);action=after['action'] or {};plan=action.get('application_plan') or {};refs=plan.get('evidence_refs',[])
            after_apps={aid:get(base+'/applications/'+aid) for aid in apps}
            allowed={'read_goal','read_application','prepare_application_action','ask_user','prepare_training','prepare_job_research','finish'}
            traces=after['steps'][-1]['tool_log'] or []
            output={'sql_committed':after['steps'][-1]['status']=='SUCCEEDED','decision_type':action.get('type'),
                'action_kind':plan.get('kind'),'evidence_refs':refs,'action_text':action.get('text'),
                'goal_status':after['status'],'goal_version':version,'plan_state':after.get('application_plan_state'),
                'training_evidence_selected':any(r.startswith('observation:') for r in refs),
                'selected_evidence_assisted':[o['payload']['assisted'] for a in after_apps.values() for o in a['training_observations'] if 'observation:'+o['id'] in refs],
                'owned_application':not plan or plan.get('application_id') in apps,
                'application_state_unchanged':all((before[aid]['version'],before[aid]['status'],before[aid]['events'])==(a['version'],a['status'],a['events']) for aid,a in after_apps.items()),
                'retracted_evidence_excluded':not retired.intersection(refs),
                'tool_scope_respected':all(t['tool'] in allowed for t in traces),'tool_log':traces,
                'artifacts':{'goal':after,'applications':after_apps}}
            result={'status':'completed' if output['sql_committed'] else 'product_failure','output':output,
                'provider_steps':after['model_requests']-setup_steps,'setup_provider_steps':setup_steps,
                'decision_elapsed_ms':elapsed,'error_code':after['steps'][-1]['error_code']}
            if live:
                summary=arun(billing_summary(settings,live_options,gid))
                result.update(model_http_calls=summary['http_attempts'],model_cost_cny=None,
                    model_cost_upper_nano=summary['accounted_upper_nano'],model_usage_status=summary['usage_status'])
                output['billing']=summary
                if summary['usage_status']=='unknown':result.update(status='authorization_stopped',error_code='model_usage_unknown')
                elif after['status']=='WAITING_CREDENTIALS':result['status']='authorization_stopped'
    except Exception as exc:
        # Harness/setup failures stay in the denominator/report, with no raw credentials or exception text.
        result={'status':'infrastructure_failure','error_code':step+'_'+type(exc).__name__}
    finally:
        if uid and not live:
            try:arun(cleanup(owner,uid))
            except Exception:result={'status':'infrastructure_failure','error_code':'synthetic_account_cleanup_failed'}
        elif uid:
            result.setdefault('output',{})['retained_resources']={'synthetic_user_id':uid,'goal_id':gid,'reason':'preserve_live_trace_and_accounting'}
            # Even setup/worker failures must retain every observed paid request and uncertain reservation.
            if gid and 'model_http_calls' not in result:
                try:
                    summary=arun(billing_summary(settings,live_options,gid))
                    result.update(model_http_calls=summary['http_attempts'],model_cost_cny=None,model_cost_upper_nano=summary['accounted_upper_nano'],model_usage_status=summary['usage_status'])
                except Exception:
                    result.update(status='authorization_stopped',error_code='billing_summary_unavailable',model_http_calls=None,
                        model_cost_cny=None,model_cost_upper_nano=None,model_usage_status='unknown')
    return result
