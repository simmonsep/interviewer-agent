"""Authenticated ASGI + real PostgreSQL, no model or external application."""
from uuid import uuid4
import pytest
from pydantic import ValidationError
from interview_agent.applications.contracts import PackageInput,Progress
from test_postgres import pg_settings,client_for,signup,query

pytestmark=pytest.mark.postgres

def post(c,path,value,key=None): return c.post(path,json=value,headers={'Idempotency-Key':key or uuid4().hex})

@pytest.fixture
def context(pg_settings):
    with client_for(pg_settings[0]) as c:
        c.headers['Origin']=pg_settings[0].origin; uid=signup(c)['user']['id']
        g=post(c,'/api/career-goals/',{'message':'准备申请','jd_text':'Java SQL原始岗位','conditions':{'direction':'Java'}}).json()
        yield c,g
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))

def url(g): return f"/api/career-goals/{g['id']}/applications"
def body(**kw): return {'expected_version':1,'expected_material_revision':0,'company':'合成公司','title':'Java实习','application_url':'https://example.com/apply',**kw}
def create(c,g,**kw):
    x=post(c,url(g),body(**kw)); assert x.status_code==201,x.text
    return x.json()
def progress(c,g,a,status='SUBMITTED',key=None,**kw):
    return post(c,url(g)+'/'+a['id']+'/progress',{'expected_application_version':a['version'],'status':status,'note':'合成用户声明，非系统投递','evidence_kind':'user_confirmation',**kw},key)

def test_create_replay_dedup_and_owner(context,pg_settings):
    c,g=context;k=uuid4().hex;a=post(c,url(g),body(),k)
    assert a.status_code==201
    assert post(c,url(g),body(),k).status_code==200
    assert post(c,url(g),body(title='其他'),k).status_code==409
    assert post(c,url(g),body()).json()['code']=='application_already_exists'
    assert len(c.get(url(g)).json()['items'])==1
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert other.get(url(g)+'/'+a.json()['id']).status_code==404
        assert post(other,url(g),body()).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))

def test_progress_replay_correction_and_frozen_package(context):
    c,g=context;a=create(c,g);k=uuid4().hex
    s=progress(c,g,a,key=k);assert s.status_code==200
    assert progress(c,g,a,key=k).json()['version']==2
    assert progress(c,g,a,key=k,note='不同声明').status_code==409
    assert progress(c,g,a).json()['code']=='application_version_conflict'
    s=s.json();assert s['submitted_package_revision']==1 and s['status']=='SUBMITTED'
    assert s['events'][0]['payload']['verification']=='user_reported_not_external_verified'
    assert s['review']['actions'][0]['kind']=='wait_for_update'
    r=post(c,url(g)+'/'+a['id']+'/packages',body(expected_application_version=2))
    assert r.json()['code']=='application_package_frozen'
    assert progress(c,g,s,'DRAFT').status_code==409
    fixed=progress(c,g,s,'DRAFT',is_correction=True,note='合成用户更正此前误记').json()
    assert fixed['status']=='DRAFT' and len(fixed['events'])==2 and fixed['submitted_package_revision']==1

def test_material_package_versions_and_later_goal_change(context):
    c,g=context
    src=post(c,f"/api/career-goals/{g['id']}/materials/sources",{'expected_version':1,'expected_revision':0,'name':'原材料.md','format':'markdown','text':'原始项目SQL'}).json()
    a=create(c,g,expected_material_revision=1,source_id=src['source_id'])
    latest=post(c,f"/api/career-goals/{g['id']}/materials/sources",{'expected_version':1,'expected_revision':1,'name':'新材料.md','format':'markdown','text':'新增Redis'}).json()
    p=post(c,url(g)+'/'+a['id']+'/packages',body(expected_application_version=1,expected_material_revision=2,source_id=latest['source_id']))
    assert p.status_code==200,p.text
    a=p.json();assert len(a['packages'])==2 and a['package']['material']['text']=='新增Redis'
    assert a['packages'][0]['payload']['material']['text']=='原始项目SQL'
    post(c,f"/api/career-goals/{g['id']}/messages",{'expected_version':1,'message':'改目标','jd_text':'Python新岗位','conditions':{'direction':'AI'}})
    v=c.get(url(g)+'/'+a['id']).json()
    assert v['package']['target']['text']=='Java SQL原始岗位'
    assert 'review_target' in {x['kind'] for x in v['review']['actions']}
    assert 'manual_submission' not in {x['kind'] for x in v['review']['actions']}

def test_closed_job_changes_advice_not_claimed_progress(context):
    c,g=context;a=create(c,g)
    v=post(c,url(g)+'/'+a['id']+'/availability',{'expected_application_version':1,'availability':'closed','note':'合成用户报告关闭'}).json()
    assert v['status']=='DRAFT'
    assert 'manual_submission' not in {x['kind'] for x in v['review']['actions']}
    assert v['review']['warnings'][0]['code']=='job_closed_user_reported'
    # History may record an earlier real submission; closure never fabricates rejection.
    s=progress(c,g,v).json();assert s['status']=='SUBMITTED'
    assert s['review']['facts'][0]['verification']=='user_reported_not_external_verified'

def test_sql_history_cannot_be_rewritten(context,pg_settings):
    c,g=context;a=create(c,g);progress(c,g,a)
    for table,col in [('career_application_packages','sha256'),('career_application_events','kind')]:
        with pytest.raises(Exception,match='career_snapshot_immutable'):
            query(pg_settings[1],f"UPDATE app.{table} SET {col}='changed' WHERE application_id=%s RETURNING *",(a['id'],))

def test_direction_only_and_evidence_validation(context):
    c,g=context
    d=post(c,'/api/career-goals/',{'message':'练习','conditions':{'direction':'Java'}}).json()
    assert post(c,url(d),body()).json()['code']=='application_job_required'
    a=create(c,g)
    assert progress(c,g,a,evidence_kind='receipt_note').status_code==400
    for bad in ['javascript:alert(1)','http://example.com','https://user:pass@example.com',' ']:
        with pytest.raises(ValidationError): PackageInput.model_validate(body(application_url=bad))
    with pytest.raises(ValidationError): Progress.model_validate({'expected_application_version':1,'status':'SUBMITTED','note':' ','evidence_kind':'user_confirmation'})
