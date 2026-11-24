"""CORE-06-01: test-only real HTTP/SQL/Redis journey, scripted roles, no model Key."""
import argparse
import asyncio
import hashlib
import json
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.contracts.operations import QueueMessage
from interview_agent.storage.config import Settings,DatabaseSettings
from interview_agent.storage.migrate import migrate
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for as career_worker
from interview_agent.career.research_repository import ResearchRepository
from interview_agent.career.research_worker import worker_for as research_worker
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.worker import worker_for as material_worker
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for as interview_worker

ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:3100'


def write(path,value):path.write_bytes((json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n').encode())


async def audit(owner):
    async with await AsyncConnection.connect(owner.conninfo()) as c:
        return {name:await (await c.execute(sql)).fetchall() for name,sql in {
            'migrations':'SELECT name,sha256 FROM infra.schema_migrations ORDER BY name',
            'calls':'SELECT row_to_json(c) FROM app.model_http_calls c ORDER BY id',
            'authorizations':'SELECT row_to_json(a) FROM app.model_authorizations a ORDER BY id'}.items()}


async def run(args):
    api=Settings.load(args.api_config);settings=WorkerSettings.load(args.worker_config)
    owner=DatabaseSettings.model_validate_json(args.migration_config.read_text(encoding='utf-8'))
    if api.mode!='test' or settings.mode!='test' or owner.dbname!='py_interview_test' or owner.user!='py_interview_owner':
        raise ValueError('explicit_test_environment_required')
    if any(getattr(api.database,k)!=getattr(settings.database,k) or getattr(api.database,k)!=getattr(owner,k) for k in ('host','port','dbname')):
        raise ValueError('database_profiles_must_match')
    with socket.socket() as s:s.bind(('127.0.0.1',3100))
    applied=await migrate(owner,ROOT/'backend/migrations');before=await audit(owner)
    identity=uuid4().hex;out=args.output/identity;out.mkdir(parents=True)
    receipt={'status':'failed','run_id':identity,'model':'scripted_protocol','source':'offline_fixture',
             'provider_http':0,'model_cost_cny':0,'real_key_reads':0,'checks':[],
             'limits':['Not model quality or human benefit evidence','No Java execution, PDF parsing or OS restart test'],
             'applied_migrations':applied}
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect();process=None;uid=None;streams=[];checkpoint='startup'
    fixture=json.loads((ROOT/'fixtures/career-research/http.json').read_text(encoding='utf-8'))
    def source(request):
        return httpx.Response(200,json=fixture['list'] if request.method=='POST' else fixture['details'][request.url.params['id']])
    repos={'career':(CareerRepository,career_worker,'career_steps'),'research':(ResearchRepository,research_worker,'career_research_runs'),
           'materials':(MaterialRepository,material_worker,'career_material_runs'),'interviews':(InterviewRepository,interview_worker,'career_interview_runs')}
    async def pump(kind,gid):
        repo_type,factory,table=repos[kind]
        async with pool.connection() as c:
            row=await (await c.execute(f"SELECT id,job_id,goal_version FROM app.{table} WHERE goal_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",(gid,))).fetchone()
        if not row:raise RuntimeError('expected_owned_pending_job_'+kind)
        stream=f'py-interview:test:{identity}:jobs:v1:{kind}'
        if stream not in streams:streams.append(stream)
        q=QueueTransport(redis,stream);repo=repo_type(pool)
        await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
        kwargs={'mode':'scripted'} if kind=='career' else {'provider':'scripted'}
        if kind=='research':kwargs.update(inner_factory=lambda:httpx.MockTransport(source),origin='offline_fixture')
        if not await factory(repo,q,**kwargs).once():raise RuntimeError('owned_worker_did_not_run')
    def check(condition,code):
        if not condition:raise AssertionError(code)
        receipt['checks'].append(code)
    try:
        await pool.open(wait=True);await redis.ping()
        with (out/'api.log').open('wb') as log:
            process=subprocess.Popen([sys.executable,'-m','interview_agent.api','--config',str(args.api_config.resolve())],
                cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
            async with httpx.AsyncClient(base_url=BASE,headers={'Origin':BASE},trust_env=False,timeout=10) as client:
                async def request(path,body=None,key=None):
                    response=await (client.get(path) if body is None else client.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex}))
                    if response.status_code>=400:raise RuntimeError('HTTP_'+str(response.status_code)+'_'+response.json().get('code','unknown'))
                    return response.json()
                until=asyncio.get_running_loop().time()+20
                while True:
                    if process.poll() is not None:raise RuntimeError('owned_api_exited')
                    try:
                        if (await client.get('/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    if asyncio.get_running_loop().time()>until:raise RuntimeError('api_start_timeout')
                    await asyncio.sleep(.2)
                signup=await request('/api/auth/sign-up/email',{'email':'core0601-'+identity+'@example.com','name':'Synthetic','password':secrets.token_urlsafe(24)})
                uid=signup['user']['id']
                checkpoint='jd_material_application'
                g=await request('/api/career-goals/',{'message':'按此JD准备Java岗位','jd_text':'Java、SQL事务和Redis并发，需解释个人贡献。','conditions':{'direction':'Java'}})
                gid=g['id'];base='/api/career-goals/'+gid
                await pump('career',gid)
                check((await request(base))['action']['type']=='prepare_training','jd_direct_training_proposal')
                await request(base+'/materials/sources',{'expected_version':1,'expected_revision':0,'name':'synthetic.md','format':'markdown',
                    'text':'我负责SQL事务和Redis通知。\n我用独立数据库验证取消后的旧结果不能提交。'})
                await request(base+'/materials/runs',{'expected_version':1,'expected_revision':1});await pump('materials',gid)
                m=await request(base+'/materials');run=m['runs'][0]
                check(not m['facts'],'proposals_not_confirmed_facts')
                await request(base+'/materials/confirmations',{'expected_version':1,'expected_revision':1,'run_id':run['id'],'proposal_ids':[0,1]})
                await request(base+'/materials/runs',{'expected_version':1,'expected_revision':2,'mode':'draft'});await pump('materials',gid)
                m=await request(base+'/materials');draft=m['drafts'][0];write(out/'01-materials.json',m)
                a=await request(base+'/applications',{'expected_version':1,'expected_material_revision':2,'company':'合成公司','title':'Java实习',
                    'draft_id':draft['id'],'application_url':'https://example.com/apply'})
                ap=base+'/applications/'+a['id']
                a=await request(ap+'/progress',{'expected_application_version':a['version'],'status':'SUBMITTED','note':'合成用户声明，未访问外部网站或自动投递','evidence_kind':'user_confirmation'})
                checkpoint='private_bank_written_and_retest'
                document={'schema_version':2,'name':'主链合成库','items':[{'item_id':'sql-transaction','kind':'single_choice','roles':['java-backend'],
                    'title':'事务与重试','question':'接口结果丢失后，怎样处理同一请求重试？','options':[{'option_id':'A','text':'直接再执行一次'},{'option_id':'B','text':'核对持久化请求身份与结果'}],
                    'correct_option_ids':['B']}]}
                p=await request('/api/question-banks/imports',{'document':document})
                bank=await request('/api/question-banks/imports/'+p['preview_id']+'/commit',{})
                async def written(option,version=1,prior=None):
                    body={'expected_version':version,'role':'java-backend','question_banks':[{'bank_id':bank['bank_id'],'version':bank['version']}],
                          'application_id':a['id'],'interview_round':'合成笔试' if not prior else '复测'}
                    if prior:body['retest_observation_id']=prior
                    s=await request(base+'/written',body);path=base+'/written/'+s['id'];q=s['questions'][0]['question_id']
                    s=(await request(path+'/answers',{'expected_version':s['version'],'question_id':q,'option_ids':[option]}))['session']
                    s=await request(path+'/finish',{'expected_version':s['version']})
                    oid=(await request(path+'/observations',{'expected_version':s['version'],'source_id':q}))['observation_id']
                    return s,oid
                first,oid=await written('A');write(out/'02-written.json',first)
                check(first['report']['declared_key_match_count']==0,'declared_key_mismatch_recorded')
                checkpoint='application_coordinator_plan'
                await request(base+'/messages',{'expected_version':1,'message':'请复盘该申请的笔试记录','intent':'review'});await pump('career',gid)
                plan=await request(base);write(out/'03-plan.json',plan)
                check(plan['action']['application_plan']['kind']=='retest_written' and plan['application_plan_state']=='current','source_bound_retest_proposal')
                second,new_oid=await written('B',2,oid)
                write(out/'02b-retest.json',second)
                memory=await request(base+'/written-memory');comparison=next(o for o in memory['items'] if o['id']==new_oid)['comparison']
                check(comparison['relation']=='same_task_repetition' and comparison['performance_improvement'] is None,'same_task_not_learning_gain')
                await request(base+'/messages',{'expected_version':2,'message':'请依据两次笔试结果重新复盘申请','intent':'review'});await pump('career',gid)
                current=await request(base);write(out/'03b-current-plan.json',current)
                check(current['application_plan_state']=='current' and 'written-observation:'+oid in current['action']['application_plan']['evidence_refs'],
                      'current_plan_references_old_basis_before_retraction')
                await request(base+f'/written-memory/{oid}/retract',{'reason':'合成用户撤回旧依据以检查失效传播'})
                check((await request(base))['application_plan_state']=='outdated','retraction_invalidates_plan')
                memory=await request(base+'/written-memory');write(out/'04-memory.json',memory)
                check(next(o for o in memory['items'] if o['id']==new_oid)['comparison']['source_active'] is False,'comparison_basis_retracted')
                checkpoint='historical_application_interview'
                s=await request(base+'/interviews',{'expected_version':3,'role':'java-backend','application_id':a['id'],'interview_round':'技术一面','max_questions':1})
                await pump('interviews',gid);path=base+'/interviews/'+s['id'];s=await request(path)
                q=next(e for e in reversed(s['events']) if e['kind']=='question')
                check(q['payload']['kind']=='project' and s['application']['package_revision']==1,'historical_package_project_question')
                await request(path+'/answers',{'expected_version':s['version'],'question_id':q['id'],'text':'我负责事务与通知，并保留独立验证记录和限制。'})
                await pump('interviews',gid);s=await request(path);write(out/'05-interview.json',s)
                check(s['status']=='COMPLETED' and len(s['observations'])==1,'whole_interview_and_observation')
                app=await request(ap);write(out/'06-application.json',app)
                check(app['status']=='SUBMITTED' and len(app['events'])==1 and app['training_observations'],'training_does_not_change_user_reported_progress')
                checkpoint='vague_intent_research_no_resume'
                b=await request('/api/career-goals/',{'message':'我想找实习，但方向还没定'});bid=b['id'];bp='/api/career-goals/'+bid
                await pump('career',bid);check((await request(bp))['action']['type']=='ask_user','vague_intent_clarification')
                await request(bp+'/messages',{'expected_version':1,'message':'先找上海Java实习','intent':'research',
                    'conditions':{'direction':'Java','cities':['上海'],'auto_research':True}})
                await pump('career',bid);await pump('research',bid)
                j=await request(bp+'/jobs');run=j['runs'][0];match=next(x for x in run['result']['matches'] if x['outcome']!='excluded')
                await request(bp+'/selection',{'expected_version':2,'run_id':run['id'],'snapshot_id':match['snapshot_id'],'acknowledge_unknown':True})
                s=await request(bp+'/interviews',{'expected_version':2,'role':'java-backend','max_questions':1});await pump('interviews',bid)
                s=await request(bp+'/interviews/'+s['id']);check(not s['material_fact_ids'] and s['target']['kind']=='selected_job','selected_job_training_without_resume')
                await request(bp+f"/interviews/{s['id']}/stop",{'expected_version':s['version']});write(out/'07-research.json',j)
                checkpoint='ai_role_without_resume_or_jd'
                b=await request('/api/career-goals/',{'message':'练AI应用面试','intent':'training','conditions':{'direction':'AI应用'}});bid=b['id'];bp='/api/career-goals/'+bid
                await pump('career',bid);v=await request(bp)
                s=await request(bp+f"/steps/{v['steps'][-1]['id']}/confirm-training",{'expected_version':1,'role':'ai-application','max_questions':1,'continue_on_completion':True})
                await pump('interviews',bid);s=await request(bp+'/interviews/'+s['id'])
                check(not s['material_fact_ids'] and s['policy']['role']=='ai-application','ai_role_without_resume_or_jd')
                await request(bp+f"/interviews/{s['id']}/stop",{'expected_version':s['version']})
                check((await request(bp))['status']=='WAITING_FOR_USER','stop_returns_user_control_without_model_wakeup')
                receipt['status']='passed'
    except Exception as error:receipt.update(failed_step=checkpoint,error_type=type(error).__name__,error_code=str(error) if isinstance(error,(AssertionError,RuntimeError)) else None)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:await asyncio.to_thread(process.wait,timeout=10)
            except subprocess.TimeoutExpired:process.kill();await asyncio.to_thread(process.wait,timeout=5)
        receipt['owned_api_stopped']=process is None or process.poll() is not None
        if uid:
            async with await AsyncConnection.connect(owner.conninfo(),autocommit=True) as c:
                rows=await (await c.execute('DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))).fetchall()
                receipt['cleaned_owned_synthetic_users']=len(rows)
        if streams:await redis.delete(*streams)
        await redis.aclose();await pool.close()
        after=await audit(owner);receipt['ledger_and_migrations_unchanged']=before==after
        receipt['ledger_snapshot_sha256']=hashlib.sha256(json.dumps(after,sort_keys=True,default=str).encode()).hexdigest()
        if before!=after:receipt['status']='failed'
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(out),'checks':len(receipt['checks']),'failed_step':receipt.get('failed_step')},ensure_ascii=False))
    return 0 if receipt['status']=='passed' else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--api-config',type=Path,required=True);p.add_argument('--worker-config',type=Path,required=True)
    p.add_argument('--migration-config',type=Path,required=True);p.add_argument('--output',type=Path,default=ROOT/'.private/core0601/http')
    try:raise SystemExit(asyncio.run(run(p.parse_args()),loop_factory=selector_loop))
    except (ValueError,OSError) as error:
        print(json.dumps({'status':'preflight_failed','error_type':type(error).__name__}));raise SystemExit(1)
