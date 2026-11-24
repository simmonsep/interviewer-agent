"""JA-08-03 user-JD career loop, real HTTP/PG/Redis and scripted Workers, test-only CNY0."""
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
    out=ROOT/'.private/ja-08-03/http'/uuid4().hex;out.mkdir(parents=True)
    email='ja0803-'+out.name+'@example.com';processes=[];logs=[]
    receipt={'status':'failed','provider':'scripted_protocol','model_http_requests':0,'new_model_cost_cny':0}
    step='startup'
    try:
        commands=[('api',[sys.executable,'-m','interview_agent.api','--config','.private/postgres/test.json'])]
        for name,module in [('career','career.worker'),('materials','materials.worker'),('interviews','interviews.worker')]:
            commands.append((name,[sys.executable,'-m','interview_agent.'+module,'--config','.private/redis/test-worker.json','--provider','scripted']))
        for name,command in commands:
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
            def wait_goal():
                until=time.monotonic()+30
                while time.monotonic()<until:
                    v=request(base)
                    if v['status'] not in ('PENDING','RUNNING'):return v
                    time.sleep(.2)
                raise RuntimeError('career_wait_timeout')
            def material_run(body):
                run=request(base+'/materials/runs',body)['run_id'];until=time.monotonic()+30
                while time.monotonic()<until:
                    v=request(base+'/materials');r=next(r for r in v['runs'] if r['id']==run)
                    if r['status']=='SUCCEEDED':return r,v
                    if r['status'] not in ('PENDING','RUNNING'):raise RuntimeError('material_'+r['status'])
                    time.sleep(.2)
                raise RuntimeError('material_wait_timeout')
            step='user_jd_and_coordination'
            request('/api/auth/sign-up/email',{'email':email,'name':'Synthetic','password':secrets.token_urlsafe(24)})
            g=request('/api/career-goals/',{'message':'按用户提供的Java岗位准备材料和面试，之后复盘申请。','jd_text':'合成Java岗位：事务、并发、Redis与接口设计。','conditions':{'direction':'Java后端'}})
            base='/api/career-goals/'+g['id'];v=wait_goal()
            assert v['action']['type']=='prepare_training';write(out/'01-goal.json',v)
            step='confirmed_material_and_draft'
            request(base+'/materials/sources',{'expected_version':1,'expected_revision':0,'name':'synthetic.md','format':'markdown',
                'text':'我负责实现SQL事务与Redis通知。\n我用独立数据库验证取消后的旧结果不能提交。'})
            r,_=material_run({'expected_version':1,'expected_revision':1,'mode':'analyze'})
            assert len(r['result']['proposals'])==2
            request(base+'/materials/confirmations',{'expected_version':1,'expected_revision':1,'run_id':r['id'],'proposal_ids':[0,1]})
            _,v=material_run({'expected_version':1,'expected_revision':2,'mode':'draft'})
            draft=v['drafts'][0];write(out/'02-materials.json',v)
            step='manual_application_record'
            a=request(base+'/applications',{'expected_version':1,'expected_material_revision':2,'company':'合成公司','title':'Java实习','draft_id':draft['id'],'application_url':'https://example.com/apply'})
            ap=base+'/applications/'+a['id']
            a=request(ap+'/progress',{'expected_application_version':a['version'],'status':'SUBMITTED','note':'合成用户确认提交；脚本未访问或代投官网','evidence_kind':'user_confirmation'})
            a=request(ap+'/progress',{'expected_application_version':a['version'],'status':'INTERVIEW','note':'用户声明进入一面，未外部核验','evidence_kind':'receipt_note','receipt_reference':'synthetic-user-note','interview_round':'技术一面'})
            write(out/'03-application.json',a)
            step='application_bound_interview'
            s=request(base+'/interviews',{'expected_version':1,'role':'java-backend','max_questions':2,'application_id':a['id'],'interview_round':'技术一面'})
            path=base+'/interviews/'+s['id'];s=wait(path)
            assert next(e for e in s['events'] if e['kind']=='question')['payload']['kind']=='project'
            for text in ('我负责事务边界和通知，并保留验证限制。','不知道，只加volatile。'):
                q=next(e for e in reversed(s['events']) if e['kind']=='question')
                request(path+'/answers',{'expected_version':s['version'],'question_id':q['id'],'text':text});s=wait(path)
            assert s['status']=='COMPLETED' and len(s['observations'])==2;write(out/'04-interview.json',s)
            step='coordinator_evidence_plan'
            body={'expected_version':1,'message':'请复盘合成公司Java实习申请，优先复测这次面试的观察。'};key=uuid4().hex
            request(base+'/messages',body,key);assert request(base+'/messages',body,key)['version']==2
            v=wait_goal();assert v['status']=='WAITING_FOR_USER' and v['application_plan_state']=='current'
            plan=v['action']['application_plan'];assert plan['kind']=='retest_topic' and plan['execution']=='proposal_only'
            assert plan['application_id']==a['id'] and plan['package_sha256']==s['application']['package_sha256']
            assert [t['tool'] for t in v['steps'][-1]['tool_log']]==['read_goal','read_application','prepare_application_action']
            assert v['action']['text']==plan['reason'];write(out/'05-proposed-plan.json',v)
            assert request(ap)['status']=='INTERVIEW' and len(request(ap)['events'])==2
            step='retract_and_replan'
            oid=next(ref.split(':')[1] for ref in plan['evidence_refs'] if ref.startswith('observation:'))
            request(base+'/interview-memory/'+oid+'/retract',{'reason':'合成用户撤回本观察，请重新依据其余证据复盘'})
            stale=request(base);assert stale['application_plan_state']=='outdated' and '已更新' in stale['action']['text'];write(out/'06-outdated.json',stale)
            request(base+'/messages',{'expected_version':2,'message':'请根据剩余有效观察重新复盘这个申请。'})
            v=wait_goal();new=v['action']['application_plan']
            assert v['application_plan_state']=='current' and new['kind']=='retest_topic'
            assert 'observation:'+oid not in new['evidence_refs'] and new['context_sha256']!=plan['context_sha256'];write(out/'07-replanned.json',v)
            assert request(ap)['status']=='INTERVIEW' and len(request(ap)['events'])==2
            receipt.update(status='passed',checks=['user_jd_skips_search','scripted_coordination','source_analysis_user_confirmation_and_draft',
                'manual_progress_is_user_claim','historical_package_project_question','whole_interview_completed','three_tool_application_coordination',
                'server_resolved_action_and_references','message_idempotency','training_retraction_marks_old_plan_outdated',
                'new_request_replans_without_retracted_observation','no_application_status_or_event_side_effect'])
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
