"""JA-05 whole-session HTTP demo, no browser. Default scripted/test; --live uses the explicit JA-05 development authorization."""
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
from interview_agent.interviews.billing import InterviewPolicy,InterviewLedger,AUTHORIZATION_ID
from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import authorize

ROOT=Path(__file__).resolve().parents[1];BASE='http://127.0.0.1:3100'


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


async def audit(live):
    settings=Settings.load(ROOT/('.private/postgres/app.json' if live else '.private/postgres/test.json'))
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            old=await (await conn.execute('''SELECT authorization_id,state,count(*) AS calls,sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END) AS upper_nano
                FROM app.model_http_calls WHERE authorization_id<>%s GROUP BY authorization_id,state ORDER BY authorization_id,state''',(AUTHORIZATION_ID,))).fetchall()
        current=await InterviewLedger(pool,InterviewPolicy.load(ROOT/'configs/ja05-deepseek-flash.json'),AUTHORIZATION_ID).summary() if live else None
        return {'current':current,'other_authorizations':old}
    finally:await pool.close()


def request(c,path,body=None,key=None):
    r=c.get(path) if body is None else c.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex})
    if r.status_code>=400:raise RuntimeError(f'{path}: HTTP {r.status_code}/{r.json().get("code")}')
    return r.json()


def wait(c,path,output,label):
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        s=request(c,path)
        if s['status'] in ('WAITING_FOR_USER','COMPLETED'):
            write(output/(label+'.json'),s);return s
        if s['status'] not in ('PENDING','RUNNING'):
            write(output/(label+'-failed.json'),s);raise RuntimeError('session stopped: '+s['status']+'/'+str(s.get('run')))
        time.sleep(.25)
    raise RuntimeError('session wait timeout')


def scenario(email,output,live):
    checks=[];sessions=[]
    with httpx.Client(base_url=BASE,headers={'Origin':BASE},trust_env=False,timeout=10) as c:
        user=request(c,'/api/auth/sign-up/email',{'email':email,'name':'JA05 Synthetic','password':secrets.token_urlsafe(24)})
        modes=['interview'] if live else ['interview','coaching']
        for mode in modes:
            g=request(c,'/api/career-goals/',{'message':'准备Java后端面试，暂不提供简历。','jd_text':'合成岗位需要Java并发、SQL事务、Redis及HTTP接口设计。','conditions':{'direction':'Java后端'}})
            base=f"/api/career-goals/{g['id']}";s=request(c,base+'/interviews',{'expected_version':1,'role':'java-backend','mode':mode,'max_questions':2})
            path=base+'/interviews/'+s['id'];s=wait(c,path,output,mode+'-initial');assert s['status']=='WAITING_FOR_USER' and not s['material_fact_ids']
            for index in range(2):
                q=next(e for e in reversed(s['events']) if e['kind']=='question');topic=q['payload']['topic_id']
                weak={'java-concurrency':'只加volatile，我不清楚复合更新。','redis-lock':'只用SETNX，释放时直接删除，其他不清楚。','sql-isolation':'事务内每次读都相同，隔离级别我不清楚。','http-idempotency':'HTTP200就表示成功，重试的事情不清楚。','agent-tools':'参数通过Schema就直接执行，归属检查不清楚。'}
                strong={'java-concurrency':'volatile不让复合读改写自动成为原子操作，需要同步或合适的原子更新，并说明测试边界。','redis-lock':'释放要原子比较唯一持有者标识再删除，锁过期后旧执行者也不能继续提交，不能只依靠TTL宣称所有业务安全。','sql-isolation':'PostgreSQL Read Committed按每条语句开始取快照，两次读之间有提交可能变化；可根据业务讨论更高隔离级别的代价。','http-idempotency':'服务端持久保存请求身份、请求体摘要与结果，相同键不同请求体拒绝；提交后丢响应的重试读取原结果。','agent-tools':'Schema验证结构，服务端仍核认证身份、归属、工具白名单、版本、执行权和预算，提交前再次检查。'}
                body={'expected_version':s['version'],'question_id':q['id'],'text':(weak if index==0 else strong)[topic]}
                key=uuid4().hex;request(c,path+'/answers',body,key);request(c,path+'/answers',body,key)
                s=wait(c,path,output,mode+f'-answer-{index+1}')
                if index==0:
                    assert s['status']=='WAITING_FOR_USER'
                    if not live:
                        new=next(e for e in reversed(s['events']) if e['kind']=='question')
                        assert new['payload']['kind']==('retest' if mode=='coaching' else 'probe')
                    if mode=='interview':assert not s['observations'] and request(c,base+'/interview-memory')['items']==[]
            assert s['status']=='COMPLETED' and s['report']['questions']==s['report']['answers']==2 and len(s['observations'])==2
            assert all(o['answer_quote'] in next(e['payload']['text'] for e in s['events'] if e['id']==o['answer_id']) for o in s['observations'])
            assert s['report']['semantic_scoring_eligible'] is False and not s['report']['unreviewed_answer_ids']
            assert request(c,path)['events']==s['events']
            if not live and mode=='coaching':assert s['report']['assisted_answers']==1 and any(e['kind']=='explanation' for e in s['events'])
            memory=request(c,base+'/interview-memory')['items'];assert len(memory)==2
            request(c,base+f"/interview-memory/{memory[0]['id']}/retract",{'reason':'合成用户撤回这条观察，后续重新核验。'})
            assert next(o for o in request(c,path)['observations'] if o['id']==memory[0]['id'])['correction']
            write(output/(mode+'-report.json'),request(c,path)['report']);sessions.append(s['id'])
            checks.extend([mode+':whole_session',mode+':answer_idempotency_and_refresh',mode+':source_bound_observations',mode+':assistance_policy',mode+':retractable_memory'])
        return {'checks':checks,'user_id':user['user']['id'],'session_ids':sessions}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    with socket.socket() as sock:sock.bind(('127.0.0.1',3100))
    owner=DatabaseSettings.model_validate_json((ROOT/('.private/postgres/migration.json' if args.live else '.private/postgres/test-migration.json')).read_text(encoding='utf-8'))
    assert owner.dbname==('py_interview' if args.live else 'py_interview_test');asyncio.run(migrate(owner,ROOT/'backend/migrations'),loop_factory=selector_loop)
    if args.live:
        load_key(ROOT/'.private/ja-05/credentials.env');asyncio.run(authorize(owner,InterviewPolicy.load(ROOT/'configs/ja05-deepseek-flash.json'),AUTHORIZATION_ID),loop_factory=selector_loop)
    rid=uuid4().hex;email=f'ja05-{rid}@example.com';out=ROOT/'.private/ja-05'/('live' if args.live else 'scripted')/rid;out.mkdir(parents=True)
    before=asyncio.run(audit(args.live),loop_factory=selector_loop);write(out/'billing-before.json',before)
    ps=[];handles=[];receipt={'status':'failed','live':args.live};error=None
    try:
        cfg='.private/postgres/app.json' if args.live else '.private/postgres/test.json';wc='.private/redis/worker.json' if args.live else '.private/redis/test-worker.json'
        worker=[sys.executable,'-m','interview_agent.interviews.worker','--config',wc,'--provider','live' if args.live else 'scripted']
        if args.live:worker+=['--credentials','.private/ja-05/credentials.env','--policy','configs/ja05-deepseek-flash.json','--authorization',AUTHORIZATION_ID]
        for name,command in [('api',[sys.executable,'-m','interview_agent.api','--config',cfg]),('interview-worker',worker)]:
            h=(out/f'{name}.log').open('wb');handles.append(h);ps.append(subprocess.Popen(command,cwd=ROOT,stdout=h,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0))
        with httpx.Client(trust_env=False,timeout=1) as c:
            deadline=time.monotonic()+20
            while True:
                if any(p.poll() is not None for p in ps):raise RuntimeError('owned service exited; see logs')
                try:
                    if c.get(BASE+'/health').status_code==200:break
                except httpx.HTTPError:pass
                if time.monotonic()>deadline:raise RuntimeError('API startup timeout')
                time.sleep(.2)
        receipt.update(scenario(email,out,args.live));receipt['status']='passed'
    except Exception as exc:error=type(exc).__name__+': '+str(exc);receipt['error']=error
    finally:
        for p in reversed(ps):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        for h in handles:h.close()
        async def cleanup():
            async with await AsyncConnection.connect(owner.conninfo()) as conn:return len(await (await conn.execute('DELETE FROM auth.users WHERE email=%s RETURNING id',(email,))).fetchall())
        receipt['cleaned_synthetic_users']=asyncio.run(cleanup(),loop_factory=selector_loop)
        after=asyncio.run(audit(args.live),loop_factory=selector_loop);write(out/'billing-final.json',after)
        receipt['old_authorizations_unchanged']=before['other_authorizations']==after['other_authorizations'];receipt['owned_processes_stopped']=all(p.poll() is not None for p in ps)
        if not receipt['old_authorizations_unchanged']:receipt['status']='failed'
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(out),'error':error},ensure_ascii=False));return 0 if receipt['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
