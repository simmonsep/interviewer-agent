"""JA-04 non-browser real HTTP demo. Default: scripted model/test DB. --live: explicit JA-04 budget/development DB."""
import argparse
import asyncio
import json
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import DatabaseSettings,Settings
from interview_agent.storage.migrate import migrate
from interview_agent.materials.billing import MaterialPolicy,MaterialLedger,AUTHORIZATION_ID
from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import authorize

ROOT=Path(__file__).resolve().parents[1]
MATERIAL='合成示例：我负责使用FastAPI实现登录后可访问的项目任务API。\n合成示例：我在项目中使用PostgreSQL保存任务，以Redis发送通知。\n合成示例：我编写取消与重复消息测试，验证旧执行结果不会重复提交。'
BASE='http://127.0.0.1:3100'


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


async def ledger_snapshot(live):
    settings=Settings.load(ROOT/('.private/postgres/app.json' if live else '.private/postgres/test.json'))
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            history=await (await conn.execute('''SELECT authorization_id,state,count(*) AS calls,
                sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END) AS upper_nano
                FROM app.model_http_calls WHERE authorization_id<>%s GROUP BY authorization_id,state ORDER BY authorization_id,state''',(AUTHORIZATION_ID,))).fetchall()
        summary=await MaterialLedger(pool,MaterialPolicy.load(ROOT/'configs/ja04-deepseek-flash.json'),AUTHORIZATION_ID).summary() if live else None
        return {'current':summary,'other_authorizations':history}
    finally:await pool.close()


def request(client,path,value=None):
    response=client.get(path) if value is None else client.post(path,json=value,headers={'Idempotency-Key':uuid4().hex})
    if response.status_code>=400:raise RuntimeError(f'{path}: HTTP {response.status_code}, {response.json().get("code")}')
    return response.json()


def wait_result(client,path,rid):
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        v=request(client,path);r=next(r for r in v['runs'] if r['id']==rid)
        if r['status']=='SUCCEEDED':return v,r
        if r['status'] not in ('PENDING','RUNNING'):raise RuntimeError(f'material task stopped: {r["status"]}/{r["error_code"]}')
        time.sleep(.25)
    raise RuntimeError('material task timeout')


def scenario(email,output):
    with httpx.Client(base_url=BASE,headers={'Origin':BASE},trust_env=False,timeout=10) as client:
        user=request(client,'/api/auth/sign-up/email',{'email':email,'name':'JA04 Synthetic','password':secrets.token_urlsafe(24)})
        goal=request(client,'/api/career-goals/',{'message':'根据合成项目材料准备Java/AI应用后端面试。','jd_text':'合成岗位：需要HTTP API、PostgreSQL事务、Redis及测试能力；请解释自己的贡献。','conditions':{'direction':'AI应用后端'}})
        path=f"/api/career-goals/{goal['id']}/materials"
        source=request(client,path+'/sources',{'expected_version':1,'expected_revision':0,'name':'synthetic-project.md','format':'markdown','text':MATERIAL})
        run=request(client,path+'/runs',{'expected_version':1,'expected_revision':1,'mode':'analyze'})
        v,r=wait_result(client,path,run['run_id']);write(output/'analysis.json',v)
        if r['result']['action']!='analysis':raise RuntimeError('model asked clarification; recorded, not a completed draft demo')
        assert not v['facts'] and r['result']['proposals']
        for proposal in r['result']['proposals']:
            assert proposal['source_id']==source['source_id'] and MATERIAL[proposal['start_char']:proposal['end_char']]==proposal['quote']
        # Explicit confirmation in this synthetic demo only; never auto-confirm a user's real resume.
        confirmed=request(client,path+'/confirmations',{'expected_version':1,'expected_revision':1,'run_id':r['id'],'proposal_ids':list(range(len(r['result']['proposals'])))})
        assert confirmed['material_revision']==2 and confirmed['facts']
        run=request(client,path+'/runs',{'expected_version':1,'expected_revision':2,'mode':'draft'})
        v,r=wait_result(client,path,run['run_id']);write(output/'draft-result.json',v)
        assert r['result']['action']=='draft' and len(v['drafts'])==1
        draft=v['drafts'][0]
        assert not draft['stale'] and draft['payload']['target']['kind']=='user_jd'
        assert all(f['quote'] in MATERIAL for f in draft['payload']['facts'])
        exported=client.get(path+f"/drafts/{draft['id']}.md");assert exported.status_code==200 and exported.text==draft['markdown']
        (output/'draft.md').write_text(exported.text,encoding='utf-8')
        refreshed=request(client,path);assert refreshed['drafts'][0]['id']==draft['id']
        return {'checks':['authenticated_http_material_input','source_bound_analysis_without_auto_confirmation','explicit_synthetic_fact_confirmation','confirmed_fact_draft_and_target_binding','immutable_export_and_refresh'],
                'user_id':user['user']['id'],'goal_id':goal['id'],'model_requests':v['material_model_requests'],'draft_id':draft['id'],'origin':r['origin']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    # Check before starting; never stop a server belonging to another task.
    with socket.socket() as sock:sock.bind(('127.0.0.1',3100))
    owner=DatabaseSettings.model_validate_json((ROOT/('.private/postgres/migration.json' if args.live else '.private/postgres/test-migration.json')).read_text(encoding='utf-8'))
    assert owner.dbname==('py_interview' if args.live else 'py_interview_test')
    asyncio.run(migrate(owner,ROOT/'backend/migrations'),loop_factory=selector_loop)
    if args.live:
        load_key(ROOT/'.private/ja-04/credentials.env')
        asyncio.run(authorize(owner,MaterialPolicy.load(ROOT/'configs/ja04-deepseek-flash.json'),AUTHORIZATION_ID),loop_factory=selector_loop)
    rid=uuid4().hex;email=f'ja04-{rid}@example.com';output=ROOT/'.private/ja-04'/('live' if args.live else 'scripted')/rid;output.mkdir(parents=True)
    before=asyncio.run(ledger_snapshot(args.live),loop_factory=selector_loop);write(output/'billing-before.json',before)
    processes=[];handles=[];receipt={'status':'failed','live':args.live};error=None
    try:
        config='.private/postgres/app.json' if args.live else '.private/postgres/test.json'
        worker_config='.private/redis/worker.json' if args.live else '.private/redis/test-worker.json'
        worker=[sys.executable,'-m','interview_agent.materials.worker','--config',worker_config,'--provider','live' if args.live else 'scripted']
        if args.live:worker+=['--credentials','.private/ja-04/credentials.env','--policy','configs/ja04-deepseek-flash.json','--authorization',AUTHORIZATION_ID]
        for name,command in [('api',[sys.executable,'-m','interview_agent.api','--config',config]),('material-worker',worker)]:
            handle=(output/f'{name}.log').open('wb');handles.append(handle)
            processes.append(subprocess.Popen(command,cwd=ROOT,stdout=handle,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0))
        with httpx.Client(trust_env=False,timeout=1) as client:
            deadline=time.monotonic()+20
            while True:
                if any(p.poll() is not None for p in processes):raise RuntimeError('owned service exited; see logs')
                try:
                    if client.get(BASE+'/health').status_code==200:break
                except httpx.HTTPError:pass
                if time.monotonic()>deadline:raise RuntimeError('API startup timeout')
                time.sleep(.2)
        receipt.update(scenario(email,output));receipt['status']='passed'
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc);receipt['error']=error
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        for h in handles:h.close()
        async def cleanup():
            async with await AsyncConnection.connect(owner.conninfo()) as conn:
                rows=await (await conn.execute('DELETE FROM auth.users WHERE email=%s RETURNING id',(email,))).fetchall()
                return len(rows)
        receipt['cleaned_synthetic_users']=asyncio.run(cleanup(),loop_factory=selector_loop)
        after=asyncio.run(ledger_snapshot(args.live),loop_factory=selector_loop);write(output/'billing-final.json',after)
        receipt['other_authorizations_unchanged']=before['other_authorizations']==after['other_authorizations']
        receipt['owned_processes_stopped']=all(p.poll() is not None for p in processes)
        if not receipt['other_authorizations_unchanged']:receipt['status']='failed'
        write(output/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(output),'error':error},ensure_ascii=False))
    return 0 if receipt['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
