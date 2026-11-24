"""JA-08-02 real HTTP + PostgreSQL/Redis + scripted interview Worker, test-only, CNY0."""
import asyncio,json,secrets,socket,subprocess,sys,time
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg import AsyncConnection
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate
from interview_agent.api.__main__ import selector_loop

ROOT=Path(__file__).resolve().parents[1];BASE='http://127.0.0.1:3100'
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

async def audit(owner,email=None):
    async with await AsyncConnection.connect(owner.conninfo(),autocommit=True) as c:
        cleaned=0
        if email:cleaned=len(await (await c.execute('DELETE FROM auth.users WHERE email=%s RETURNING id',(email,))).fetchall())
        rows=await (await c.execute('SELECT id::text,state,reserved_nano,accounted_nano FROM app.model_http_calls ORDER BY id')).fetchall()
        return rows,cleaned

def main():
    with socket.socket() as s:s.bind(('127.0.0.1',3100))
    owner=DatabaseSettings.model_validate_json((ROOT/'.private/postgres/test-migration.json').read_text())
    assert owner.dbname=='py_interview_test'
    asyncio.run(migrate(owner,ROOT/'backend/migrations'),loop_factory=selector_loop)
    before,_=asyncio.run(audit(owner),loop_factory=selector_loop)
    out=ROOT/'.private/ja-08-02/http'/uuid4().hex;out.mkdir(parents=True)
    email='ja0802-'+out.name+'@example.com';processes=[];logs=[]
    receipt={'status':'failed','provider':'scripted_protocol','model_http_requests':0,'new_model_cost_cny':0}
    step='startup'
    try:
        for name,command in [('api',[sys.executable,'-m','interview_agent.api','--config','.private/postgres/test.json']),
                             ('worker',[sys.executable,'-m','interview_agent.interviews.worker','--config','.private/redis/test-worker.json','--provider','scripted'])]:
            log=(out/(name+'.log')).open('wb');logs.append(log)
            processes.append(subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0))
        with httpx.Client(base_url=BASE,headers={'Origin':BASE},trust_env=False,timeout=10) as c:
            def request(p,b=None,key=None):
                r=c.get(p) if b is None else c.post(p,json=b,headers={'Idempotency-Key':key or uuid4().hex})
                if r.status_code>=400:raise RuntimeError('HTTP_'+str(r.status_code)+'_'+r.json().get('code','unknown'))
                return r.json()
            deadline=time.monotonic()+20
            while True:
                if any(p.poll() is not None for p in processes):raise RuntimeError('owned_service_exited')
                try:
                    if c.get('/health').status_code==200:break
                except httpx.HTTPError:pass
                if time.monotonic()>deadline:raise RuntimeError('api_start_timeout')
                time.sleep(.2)
            def wait(path):
                until=time.monotonic()+30
                while time.monotonic()<until:
                    s=request(path)
                    if s['status'] in ('WAITING_FOR_USER','COMPLETED'):return s
                    if s['status'] not in ('PENDING','RUNNING'):raise RuntimeError('interview_'+s['status'])
                    time.sleep(.2)
                raise RuntimeError('worker_wait_timeout')
            step='prepare_application'
            request('/api/auth/sign-up/email',{'email':email,'name':'Synthetic','password':secrets.token_urlsafe(24)})
            g=request('/api/career-goals/',{'message':'原Java岗位申请','jd_text':'Java SQL原始JD','conditions':{'direction':'Java'}})
            base='/api/career-goals/'+g['id']
            a=request(base+'/applications',{'expected_version':1,'expected_material_revision':0,'company':'合成公司','title':'Java实习'})
            ap=base+'/applications/'+a['id']
            request(ap+'/progress',{'expected_application_version':1,'status':'SUBMITTED','note':'合成用户确认，非系统代投','evidence_kind':'user_confirmation'})
            request(base+'/messages',{'expected_version':1,'message':'当前改AI方向','jd_text':'Python新版JD','conditions':{'direction':'AI'}})
            step='first_round'
            b={'expected_version':2,'role':'java-backend','max_questions':2,'application_id':a['id'],'interview_round':'一面'};key=uuid4().hex
            s=request(base+'/interviews',b,key);path=base+'/interviews/'+s['id']
            assert request(base+'/interviews',b,key)['id']==s['id']
            s=wait(path);assert s['target']['text']=='Java SQL原始JD' and s['application']['package_revision']==1
            assert not s['material_fact_ids']
            request(base+'/materials/sources',{'expected_version':2,'expected_revision':0,'name':'new','format':'text','text':'新材料不应改变历史包'})
            assert request(path)['status']=='WAITING_FOR_USER'
            write(out/'round1-initial.json',s)
            for n,text in enumerate(('不知道，只加volatile。','复合更新需要原子性，并验证同步边界。')):
                q=next(e for e in reversed(s['events']) if e['kind']=='question')
                request(path+'/answers',{'expected_version':s['version'],'question_id':q['id'],'text':text})
                s=wait(path)
                if n==0:
                    v=request(ap);assert not v['training_observations'] and not v['review']['training_hypotheses']
                    write(out/'active-hidden.json',v)
            assert s['status']=='COMPLETED';write(out/'round1-complete.json',s)
            v=request(ap);assert len(v['training_observations'])==2 and v['status']=='SUBMITTED'
            assert any(x['kind']=='retest_topic' for x in v['review']['actions'])
            assert all(x['verification']=='training_observation_not_rejection_cause' for x in v['review']['training_hypotheses'])
            write(out/'reflection.json',v)
            oid=v['training_observations'][0]['id']
            request(base+'/interview-memory/'+oid+'/retract',{'reason':'合成用户撤回观察'})
            v=request(ap);assert len(v['training_observations'])==1;write(out/'retracted.json',v)
            step='second_round'
            s=request(base+'/interviews',{**b,'application_package_revision':1,'interview_round':'二面'})
            path=base+'/interviews/'+s['id'];s=wait(path)
            s=request(path+'/stop',{'expected_version':s['version']})
            v=request(ap);assert {x['binding']['interview_round'] for x in v['training_sessions']}=={'一面','二面'}
            assert len(v['training_observations'])==1;write(out/'round2-stopped.json',v)
            receipt.update(status='passed',checks=['authenticated_application_binding','create_replay','historical_jd_after_goal_change',
                'later_material_does_not_stale','no_resume_role_training','active_observations_hidden','completed_reflection_cites_evidence',
                'retraction_removes_hypothesis','rounds_persist_separately','training_never_updates_application_status'])
    except Exception as exc:receipt.update(error=type(exc).__name__,failed_step=step)
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        for log in logs:log.close()
        after,cleaned=asyncio.run(audit(owner,email),loop_factory=selector_loop)
        receipt.update(owned_processes_stopped=all(p.poll() is not None for p in processes),cleaned_synthetic_users=cleaned,model_ledger_unchanged=before==after)
        if before!=after:receipt['status']='failed'
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(out),'failed_step':receipt.get('failed_step')},ensure_ascii=False))
    return 0 if receipt['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
