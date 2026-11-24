"""One resume rewrite, followed by synthetic user revision; SDK mock by default."""
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
from interview_agent.materials.agent import ScriptedMaterials
from interview_agent.materials.billing import MaterialPolicy,MaterialLedger
from interview_agent.runtime.types import ToolCall

ROOT=Path(__file__).resolve().parents[1]
PHASE='core-06-14-20261103-cny1'
STAGES={'core-06-14':(PHASE,'core0614'),'core-06-15':('core-06-15-20261103-cny1','core0615')}
LIMIT=1_000_000_000
JD='Java后端实习：Spring Boot、SQL事务、接口幂等、Redis；能够说明模块职责与测试方法。'
SOURCE='我做了一个订单服务，项目是我负责完成的。大致用Spring Boot、MySQL和Redis，实现下单和重复请求处理。项目还不完整，细节和测试还需要完善。希望帮我整理项目经历并补充实现思路，不要先盘问是否都是我做的。'


def write(p,v):p.write_bytes((json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n').encode())


def preflight(a):
    if getattr(a,'stage','core-06-14') not in STAGES:raise ValueError('unsupported_stage')
    live=a.mode=='live'
    if live and a.mock_unknown:raise ValueError('live_must_not_use_mock_faults')
    if live and (not a.execute_live or a.max_cny!='1' or not a.credentials):raise ValueError('live_requires_explicit_stage_approval_flags')
    if not live and a.credentials:raise ValueError('mock_must_not_read_real_credentials')
    api=Settings.load(a.api_config);worker=WorkerSettings.load(a.worker_config)
    owner=DatabaseSettings.model_validate_json(a.owner_config.read_text(encoding='utf-8'))
    expected='development' if live else 'test'
    if api.mode!=expected or worker.mode!=expected or owner.user!='py_interview_owner':raise ValueError('phase_environment_mismatch')
    if any(getattr(api.database,k)!=getattr(worker.database,k) or getattr(api.database,k)!=getattr(owner,k) for k in ('host','port','dbname')):raise ValueError('database_profiles_must_match')
    if owner.dbname!=('py_interview' if live else 'py_interview_test'):raise ValueError('phase_database_mismatch')
    return api,worker,owner


async def run(a):
    api,settings,owner=preflight(a);live=a.mode=='live';nonce=uuid4().hex
    stage=getattr(a,'stage','core-06-14');phase,profile=STAGES[stage]
    out=(a.output or ROOT/'.private'/profile)/nonce;out.mkdir(parents=True)
    identity=phase if live else 'mock-'+phase+'-'+nonce
    policy_path=ROOT/f'configs/{profile}-materials.json'
    policy=MaterialPolicy.load(policy_path)
    credentials=a.credentials
    if not live:
        credentials=out/'mock.env';credentials.write_bytes(b'DEEPSEEK_API_KEY=offline-not-real\n')
    files=[*sorted((ROOT/'backend/src').rglob('*.py')),Path(__file__).resolve(),policy_path,ROOT/'uv.lock']
    frozen={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    write(out/'plan.json',{'phase':phase,'mode':a.mode,'source':SOURCE,'jd':JD,'policy':policy.model_dump(),
        'authorization':identity,'max_nano':LIMIT,'max_http':6,'paid_tasks':1,'source_sha256':frozen,
        'mock_read_batch':stage=='core-06-15' and not live,
        'human_simulation':'edit punctuation then accept; not real user adoption or quality review'})
    receipt={'status':'not_completed','mode':a.mode,'provider_http':None if live else 0,'human_review':'pending',
        'domain_success_rate':None,'accounts_retained':live,'authorization':identity}
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect();uid=None;streams=[];ready=False
    async def pump(kind,gid):
        repo=CareerRepository(pool) if kind=='career' else MaterialRepository(pool)
        table='career_steps' if kind=='career' else 'career_material_runs'
        async with pool.connection() as c:
            row=await (await c.execute(f"SELECT * FROM app.{table} WHERE goal_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",(gid,))).fetchone()
        if not row:raise RuntimeError('owned_pending_task_required')
        stream=f'py-interview:{settings.mode}:{nonce}:jobs:v1:{kind}';streams.append(stream)
        q=QueueTransport(redis,stream)
        await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
        if kind=='career':worker=career_worker(repo,q,mode='scripted')
        else:
            scope=row['input_payload']
            async def noop():pass
            scripted=ScriptedMaterials(scope,noop)
            async def mock(request):
                response=await scripted.complete([],[])
                if stage=='core-06-15' and scripted.n==2:
                    response.tool_calls.append(ToolCall(id='batch-source',name='read_source',arguments={'source_id':scope['sources'][0]['id']}))
                    scripted.n+=1  # This batch already delivered the next scripted source read.
                call=response.tool_calls[0]
                if call.name=='submit_rewrite':
                    call.arguments={'items':[{'text':'基于Spring Boot、MySQL与Redis开发订单服务，完成下单与重复请求处理，围绕订单链路组织模块职责。',
                        'source_ids':[scope['sources'][0]['id']],'change_note':'将框架与业务行动整理为项目条目。'}],
                        'suggestions':['可设计幂等键与冲突响应，并补充重复请求的测试；选择符合项目的方案完善细节。']}
                usage={'prompt_tokens':100,'completion_tokens':100,'total_tokens':200,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100}
                body={'model':policy.model,'choices':[{'message':{'role':'assistant','tool_calls':[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments,ensure_ascii=False)}} for c in response.tool_calls]},'finish_reason':'tool_calls'}]}
                if not a.mock_unknown:body['usage']=usage
                return httpx.Response(200,json=body)
            kw={} if live else {'inner_factory':lambda:httpx.MockTransport(mock)}
            worker=material_worker(repo,q,provider='live',policy=policy,authorization=identity,credentials=credentials,**kw)
        if not await worker.once():raise RuntimeError('owned_task_not_taken')
    try:
        await pool.open(wait=True);ready=True;await redis.ping()
        async with await AsyncConnection.connect(owner.conninfo()) as c:
            if await (await c.execute('SELECT id FROM app.model_authorizations WHERE id=%s',(identity,))).fetchone():raise RuntimeError('stage_already_initialized_do_not_retry')
        await authorize(owner,policy,identity,LIMIT)
        with TestClient(create_app(api),base_url=api.origin,backend_options={'loop_factory':selector_loop},raise_server_exceptions=False) as client:
            client.headers['Origin']=api.origin
            def req(path,body=None):
                r=client.get(path) if body is None else client.post(path,json=body,headers={'Idempotency-Key':uuid4().hex})
                if r.status_code>=400:raise RuntimeError('HTTP_'+str(r.status_code))
                return r.json()
            uid=req('/api/auth/sign-up/email',{'email':identity+'@example.com','name':'Synthetic','password':secrets.token_urlsafe(24)})['user']['id']
            g=req('/api/career-goals/',{'message':'先优化项目简历，不开始面试。','jd_text':JD,'conditions':{'direction':'Java'}})
            gid=g['id'];base='/api/career-goals/'+gid;receipt.update(user_id=uid,goal_id=gid)
            await pump('career',gid)
            req(base+'/materials/sources',{'expected_version':1,'expected_revision':0,'name':'synthetic.md','format':'markdown','text':SOURCE})
            m=req(base+'/materials/rewrite',{'expected_version':1,'expected_revision':1});receipt['run_id']=m['run_id']
            try:await pump('materials',gid)
            finally:write(out/'01-materials.json',req(base+'/materials'))
            fees=await MaterialLedger(pool,policy,identity).summary();write(out/'fees.json',fees)
            if fees['usage_status']=='unknown':raise RuntimeError('unknown_usage_paused_no_retry')
            m=req(base+'/materials');r=next(r for r in m['runs'] if r['id']==receipt['run_id'])
            if r['status']!='SUCCEEDED' or not m['drafts'] or m['facts']:raise RuntimeError('rewrite_without_confirmations_required')
            d=m['drafts'][0]
            if d['payload'].get('draft_style')!='rewrite' or '未核验' in d['markdown'] or '事实 ' in d['markdown']:raise RuntimeError('clean_rewrite_required')
            body={'expected_version':1,'expected_revision':1,'expected_draft_version':d['version'],'action':'edit',
                'items':[{**i,'text':i['text'].rstrip('。')+'。','change_note':'合成用户仅统一句末标点，非真人采纳。'} for i in d['payload']['rewrite_items']]}
            edited=req(base+f"/materials/drafts/{d['id']}/revisions",body);write(out/'02-synthetic-edit.json',edited)
            e=edited['drafts'][0];accepted=req(base+f"/materials/drafts/{e['id']}/revisions",{'expected_version':1,'expected_revision':1,'expected_draft_version':e['version'],'action':'accept'})
            write(out/'03-synthetic-accept.json',accepted)
            export=client.get(base+f"/materials/drafts/{accepted['draft_id']}.md")
            if export.status_code!=200 or export.text!=accepted['drafts'][0]['markdown']:raise RuntimeError('export_mismatch')
            (out/'04-export.md').write_bytes(export.content)
            receipt['status']='rewrite_protocol_passed_quality_pending'
    except Exception as error:
        receipt.update(error_type=type(error).__name__,error_code=str(error) if isinstance(error,RuntimeError) else None)
    finally:
        if ready:
            try:receipt['fees']=await MaterialLedger(pool,policy,identity).summary()
            except Exception:receipt['fee_summary_unavailable']=True
        if streams:await redis.delete(*streams)
        await redis.aclose();await pool.close()
        receipt['source_freeze_unchanged']=frozen=={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        write(out/'human-review.json',{'status':'pending','domain_success_rate':None,
            'rubrics':['先给可用初稿而非质疑用户职责','文案较原框架有实际组织改进','主动技术建议具体且可用','正文与技术补充区分、未虚构成果数字'],
            'plan_sha256':hashlib.sha256((out/'plan.json').read_bytes()).hexdigest(),
            'output_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('0*')}})
        if not live:
            async with await AsyncConnection.connect(owner.conninfo(),autocommit=True) as c:
                if uid:await c.execute('DELETE FROM auth.users WHERE id=%s',(uid,))
                await c.execute('DELETE FROM app.model_http_calls WHERE authorization_id=%s',(identity,))
                await c.execute('DELETE FROM app.model_authorizations WHERE id=%s',(identity,))
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(out)},ensure_ascii=False))
    return 0 if receipt['status']=='rewrite_protocol_passed_quality_pending' else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['sdk-mock','live'],default='sdk-mock')
    p.add_argument('--stage',choices=list(STAGES),default='core-06-14')
    for n in ['api-config','worker-config','owner-config']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--credentials',type=Path);p.add_argument('--execute-live',action='store_true');p.add_argument('--max-cny')
    p.add_argument('--mock-unknown',action='store_true');p.add_argument('--output',type=Path)
    raise SystemExit(asyncio.run(run(p.parse_args()),loop_factory=selector_loop))
