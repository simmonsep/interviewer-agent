"""JA-08-01 real local HTTP demo, test DB only, no model/Worker/external submission."""
import asyncio,json,secrets,socket,subprocess,sys,time
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg import AsyncConnection
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate
from interview_agent.api.__main__ import selector_loop
ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:3100'

def write(p,x): p.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

async def audit(owner,uid=None):
    async with await AsyncConnection.connect(owner.conninfo(),autocommit=True) as c:
        if uid: await c.execute('DELETE FROM auth.users WHERE id=%s',(uid,))
        return await (await c.execute('SELECT id::text,state,reserved_nano,accounted_nano FROM app.model_http_calls ORDER BY id')).fetchall()

def main():
    with socket.socket() as s: s.bind(('127.0.0.1',3100))
    owner=DatabaseSettings.model_validate_json((ROOT/'.private/postgres/test-migration.json').read_text())
    assert owner.dbname=='py_interview_test'
    asyncio.run(migrate(owner,ROOT/'backend/migrations'),loop_factory=selector_loop)
    before=asyncio.run(audit(owner),loop_factory=selector_loop)
    out=ROOT/'.private/ja-08/http'/uuid4().hex;out.mkdir(parents=True)
    receipt={'status':'failed','model_http_requests':0,'scope':'real authenticated HTTP, PostgreSQL; no browser/model/Worker/external submission'}
    uid=None;process=None
    with (out/'api.log').open('wb') as log:
        try:
            process=subprocess.Popen([sys.executable,'-m','interview_agent.api','--config','.private/postgres/test.json'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
            with httpx.Client(base_url=BASE,headers={'Origin':BASE},trust_env=False,timeout=10) as c:
                until=time.monotonic()+20
                while True:
                    if process.poll() is not None: raise RuntimeError('owned_api_exited')
                    try:
                        if c.get('/health').status_code==200: break
                    except httpx.HTTPError: pass
                    if time.monotonic()>until: raise RuntimeError('api_timeout')
                    time.sleep(.2)
                def post(p,b,key=None):
                    x=c.post(p,json=b,headers={'Idempotency-Key':key or uuid4().hex});assert x.status_code<400,(p,x.status_code);return x.json()
                u=post('/api/auth/sign-up/email',{'email':'ja08-'+out.name+'@example.com','name':'Synthetic','password':secrets.token_urlsafe(24)});uid=u['user']['id']
                g=post('/api/career-goals/',{'message':'合成申请','jd_text':'Java SQL原岗位','conditions':{'direction':'Java'}});gid=g['id'];p='/api/career-goals/'+gid
                src=post(p+'/materials/sources',{'expected_version':1,'expected_revision':0,'name':'合成.md','format':'markdown','text':'原始SQL项目材料'})
                b={'expected_version':1,'expected_material_revision':1,'company':'合成公司','title':'Java实习','application_url':'https://example.com/apply','source_id':src['source_id']};key=uuid4().hex
                a=post(p+'/applications',b,key);assert post(p+'/applications',b,key)['id']==a['id'];ap=p+'/applications/'+a['id']
                status={'expected_application_version':1,'status':'SUBMITTED','note':'合成用户确认已提交，非系统操作','evidence_kind':'user_confirmation'};key=uuid4().hex
                a=post(ap+'/progress',status,key);assert post(ap+'/progress',status,key)['version']==2
                assert a['submitted_package_revision']==1 and len(a['events'])==1
                write(out/'submitted.json',a)
                post(p+'/materials/sources',{'expected_version':1,'expected_revision':1,'name':'新.md','format':'markdown','text':'新材料不覆盖原投递版'})
                post(p+'/messages',{'expected_version':1,'message':'改AI岗位','jd_text':'Python新JD','conditions':{'direction':'AI'}})
                a=c.get(ap).json();assert a['package']['target']['text']=='Java SQL原岗位' and a['package']['material']['text']=='原始SQL项目材料'
                assert a['status']=='SUBMITTED' and any(x['code']=='target_changed' for x in a['review']['warnings'])
                write(out/'historical.json',a)
                d=post(p+'/applications',{'expected_version':2,'expected_material_revision':2,'company':'合成公司','title':'AI实习'})
                d=post(p+'/applications/'+d['id']+'/availability',{'expected_application_version':1,'availability':'closed','note':'用户报告关闭，未外部核验'})
                assert d['status']=='DRAFT' and 'manual_submission' not in {x['kind'] for x in d['review']['actions']}
                write(out/'closed.json',d)
                receipt.update(status='passed',checks=['authenticated_create','idempotent_create_and_progress','submitted_package_pinned','goal_and_material_changes_preserve_history','no_response_not_rejection','closed_job_changes_advice_only'])
        except Exception as exc: receipt['error']=type(exc).__name__
        finally:
            if process and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill();process.wait(timeout=5)
            after=asyncio.run(audit(owner,uid),loop_factory=selector_loop)
            receipt.update(owned_api_stopped=process is None or process.poll() is not None,synthetic_user_cleaned=uid is not None,model_ledger_unchanged=after==before)
            if after!=before: receipt['status']='failed'
            write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'receipt':str(out/'receipt.json')},ensure_ascii=False))
    return 0 if receipt['status']=='passed' else 1

if __name__=='__main__': raise SystemExit(main())
