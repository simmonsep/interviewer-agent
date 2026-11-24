"""JA-06 actual HTTP + SQL/Redis Worker + Docker demo. No browser, model or API Key."""
import argparse
import asyncio
import json
import runpy
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg import AsyncConnection
from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate
from interview_agent.execution.docker_java import DockerProfile,DockerJava

ROOT=Path(__file__).resolve().parents[1];BASE='http://127.0.0.1:3100'


def write(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


def request(c,path,body=None,key=None):
    r=c.get(path) if body is None else c.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex})
    if r.status_code>=400: raise RuntimeError(f'{path}: {r.status_code}/{r.json().get("code")}')
    return r.json()


def wait(c,path):
    until=time.monotonic()+120
    while time.monotonic()<until:
        s=request(c,path)
        if s['submissions'] and s['submissions'][-1]['status'] not in ('PENDING','RUNNING'): return s
        time.sleep(.25)
    raise RuntimeError('written execution timeout')


def scenario(email,out,profile):
    checks=[];ids=[]
    with httpx.Client(base_url=BASE,headers={'Origin':BASE},trust_env=False,timeout=10) as c:
        request(c,'/api/auth/sign-up/email',{'name':'JA06 Synthetic','email':email,'password':secrets.token_urlsafe(24)})
        for task_id,file in [('idempotency-record','IdempotencyMain.java'),('lock-owner-expiry','LockMain.java')]:
            g=request(c,'/api/career-goals/',{'message':'无简历，练习Java后端笔试。','conditions':{'direction':'Java后端'}})
            base=f"/api/career-goals/{g['id']}";s=request(c,base+'/written',{'expected_version':1,'task_id':task_id})
            path=base+'/written/'+s['id'];ids.append(s['id'])
            assert s['status']=='ACTIVE' and not s['report'] and 'hidden-1' not in str(s)
            # This deliberately wrong program prints PASS: the host judge must reject it.
            for index,source in enumerate(['public class Main { public static void main(String[] a) { System.out.println("PASS"); } }',
                                          (ROOT/'fixtures/ja06'/file).read_text(encoding='utf-8')]):
                key=uuid4().hex;body={'expected_version':s['version'],'source':source}
                a=request(c,path+'/submissions',body,key);b=request(c,path+'/submissions',body,key)
                assert a['submission_id']==b['submission_id']
                s=wait(c,path);write(out/f'{task_id}-submission-{index+1}.json',s)
                assert len(s['submissions'])==index+1 and s['submissions'][-1]['source']==source and 'hidden-1' not in str(s)
                if s['submissions'][-1]['status']!='SUCCEEDED': raise RuntimeError(str(s['submissions'][-1]))
            s=request(c,path+'/finish',{'expected_version':s['version']});write(out/f'{task_id}-report.json',s)
            assert s['submissions'][0]['result']['verdict']=='WRONG_ANSWER'
            assert s['submissions'][1]['result']['verdict']=='ACCEPTED'
            assert request(c,path)['submissions']==s['submissions'] and s['report']['graded_submissions']==2
            assert not s['policy']['system_ai_help'] and not s['report']['ability_certified']
            checks.extend([task_id+':frozen_task_and_no_resume',task_id+':original_code_idempotency',task_id+':active_hidden_policy',
                           task_id+':actual_compilation_and_host_grading',task_id+':finish_report_and_refresh'])
        g=request(c,'/api/career-goals/',{'message':'合成取消场景','conditions':{'direction':'Java后端'}})
        base=f"/api/career-goals/{g['id']}";s=request(c,base+'/written',{'expected_version':1})
        path=base+'/written/'+s['id'];ids.append(s['id'])
        source='public class Main { public static void main(String[] a) { while(true) {} } }'
        request(c,path+'/submissions',{'expected_version':s['version'],'source':source})
        executor=DockerJava(profile)
        async def java_running():
            names=(await executor.must(['ps','-q','--filter','label=interview-agent.executor=ja06'])).decode().split()
            for name in names:
                r=await executor.command(['top',name,'-eo','pid,args'])
                if b'java -Xms' in r.stdout:return True
            return False
        until=time.monotonic()+30
        while not asyncio.run(java_running()):
            if time.monotonic()>until:raise RuntimeError('cancel demo did not observe Java runtime')
            time.sleep(.1)
        s=request(c,path);s=request(c,path+'/cancel',{'expected_version':s['version']})
        assert s['status']=='CANCELLED' and s['submissions'][0]['status']=='CANCELLED'
        until=time.monotonic()+10
        while asyncio.run(executor.must(['ps','-aq','--filter','label=interview-agent.executor=ja06'])).strip():
            if time.monotonic()>until:raise RuntimeError('cancelled candidate container not removed')
            time.sleep(.2)
        write(out/'cancel-report.json',request(c,path));checks.extend(['cancel:SQL_fence_during_Java_runtime','cancel:actual_container_cleanup'])
    return {'checks':checks,'session_ids':ids}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--profile',type=Path,default=ROOT/'configs/ja06-docker-java.json');args=parser.parse_args()
    with socket.socket() as sock: sock.bind(('127.0.0.1',3100))
    # Required ordering: prove the boundary before candidate code is ever sent to the Worker.
    check=runpy.run_path(str(ROOT/'scripts/check-executor.py'))['check']
    isolation=asyncio.run(check(DockerProfile.load(args.profile)))
    owner=DatabaseSettings.model_validate_json((ROOT/'.private/postgres/test-migration.json').read_text(encoding='utf-8'))
    if owner.dbname!='py_interview_test': raise ValueError('synthetic_demo_requires_test_database')
    asyncio.run(migrate(owner,ROOT/'backend/migrations'),loop_factory=selector_loop)
    rid=uuid4().hex;email=f'ja06-{rid}@example.com';out=ROOT/'.private/ja-06/http'/rid;out.mkdir(parents=True)
    write(out/'isolation.json',isolation)
    ps=[];handles=[];receipt={'status':'failed','model_calls':0,'model_key_read':False,'run_id':rid};error=None
    try:
        commands=[('api',[sys.executable,'-m','interview_agent.api','--config','.private/postgres/test.json','--written-executor-profile',str(args.profile)]),
                  ('written-worker',[sys.executable,'-m','interview_agent.written.worker','--config','.private/redis/test-worker.json','--executor','docker','--profile',str(args.profile)])]
        for name,command in commands:
            h=(out/f'{name}.log').open('wb');handles.append(h)
            ps.append(subprocess.Popen(command,cwd=ROOT,stdout=h,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0))
        with httpx.Client(trust_env=False,timeout=1) as c:
            until=time.monotonic()+20
            while True:
                if any(p.poll() is not None for p in ps): raise RuntimeError('owned service exited; see logs')
                try:
                    if c.get(BASE+'/health').status_code==200: break
                except httpx.HTTPError: pass
                if time.monotonic()>until: raise RuntimeError('API startup timeout')
                time.sleep(.2)
        receipt.update(scenario(email,out,DockerProfile.load(args.profile)));receipt['status']='passed'
    except Exception as e: error=type(e).__name__+': '+str(e);receipt['error']=error
    finally:
        for p in reversed(ps):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=10)
                except subprocess.TimeoutExpired: p.kill();p.wait(timeout=5)
        for h in handles: h.close()
        async def cleanup():
            async with await AsyncConnection.connect(owner.conninfo()) as conn:
                return len(await (await conn.execute('DELETE FROM auth.users WHERE email=%s RETURNING id',(email,))).fetchall())
        receipt['cleaned_synthetic_users']=asyncio.run(cleanup(),loop_factory=selector_loop)
        receipt['owned_processes_stopped']=all(p.poll() is not None for p in ps)
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(out),'error':error},ensure_ascii=False))
    return 0 if receipt['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
