"""Historical application, canonical coordinator actions and linked attempts."""
from uuid import uuid4
import pytest
from test_postgres import arun,pg_settings,query,client_for,signup
from test_worker import worker_settings
from test_application_repository import context,create as application,progress,url,body
from test_written_question_sets import create,answer,end,base,post,view
from test_written_memory import publish,memories
from test_application_coordinator import process,goal
from test_written_repository import written,create as java,submit,process as grade,FakeExecutor,view as java_view,end as java_end

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def finished(c,g,**kwargs):
    r=create(c,g,**kwargs);assert r.status_code==202,r.text
    s=r.json();s=answer(c,g,s,s['questions'][0],['A']).json()['session']
    return end(c,g,s).json()


def observation(c,g,s,index=0):
    r=publish(c,g,s,s['questions'][index]['question_id']);assert r.status_code==201,r.text
    return r.json()['observation_id']


def test_historical_package_round_coordinator_and_retraction(context,worker_settings,pg_settings):
    c,g=context;a=application(c,g);a=progress(c,g,a).json()
    s=finished(c,g,application_id=a['id'],interview_round='笔试一轮');oid=observation(c,g,s)
    app=c.get(url(g)+'/'+a['id']).json()
    assert app['status']=='SUBMITTED' and app['written_observations'][0]['application']['interview_round']=='笔试一轮'
    assert app['written_observations'][0]['application']['package_sha256']==s['application']['package_sha256']
    assert 'source' not in app['written_observations'][0]
    assert any(x['kind']=='retest_written' and 'written-observation:'+oid in x['evidence_refs'] for x in app['review']['actions'])
    assert c.get(base(g)+'/workflow').json()['written_observations'][0]['id']==oid
    arun(process(worker_settings));v=goal(c,g)
    assert v['action']['application_plan']['kind']=='retest_written' and v['application_plan_state']=='current'
    before=query(pg_settings[1],'SELECT candidate FROM app.career_steps WHERE goal_id=%s',(g['id'],))[0]['candidate']
    post(c,base(g)+f'/written-memory/{oid}/retract',{'reason':'复核声明答案'})
    assert goal(c,g)['application_plan_state']=='outdated'
    assert query(pg_settings[1],'SELECT candidate FROM app.career_steps WHERE goal_id=%s',(g['id'],))[0]['candidate']==before
    app=c.get(url(g)+'/'+a['id']).json()
    assert not app['written_observations'] and app['status']=='SUBMITTED'
    assert not any(x['kind']=='retest_written' for x in app['review']['actions'])


def test_package_selection_freeze_and_request_identity(context):
    c,g=context;a=application(c,g);a=progress(c,g,a).json();a=progress(c,g,a,'DRAFT',is_correction=True).json()
    r=post(c,url(g)+'/'+a['id']+'/packages',body(expected_application_version=a['version'],application_url='https://example.com/new-entry'))
    assert r.status_code==200,r.text;a=r.json()
    assert a['current_package_revision']==2 and a['submitted_package_revision']==1
    original=a['packages'][0]['payload']['target']
    post(c,base(g)+'/messages',{'expected_version':1,'message':'改为AI方向','jd_text':'Python最新JD'})
    from test_written_question_sets import DEFAULT
    key=uuid4().hex;payload={'expected_version':2,'role':'java-backend','question_banks':[{'bank_id':DEFAULT,'version':1}],
                           'application_id':a['id'],'interview_round':'历史笔试'}
    r=post(c,base(g)+'/written',payload,key);assert r.status_code==202,r.text;s=r.json()
    assert s['application']['package_revision']==1 and s['target']==original and s['status']=='ACTIVE'
    assert post(c,base(g)+'/written',payload,key).status_code==200
    assert post(c,base(g)+'/written',{**payload,'interview_round':'另轮'},key).status_code==409
    assert view(c,g,s)['status']=='ACTIVE';s=end(c,g,s).json()
    r=create(c,g,expected_version=2,application_id=a['id'],application_package_revision=2,interview_round='新版笔试')
    assert r.status_code==202 and r.json()['application']['package_revision']==2


def test_linked_same_and_different_attempts_never_assert_learning_gain(context):
    c,g=context;s=finished(c,g);oid=observation(c,g,s)
    second=finished(c,g,retest_observation_id=oid);second_id=observation(c,g,second)
    m=next(x for x in memories(c,g) if x['id']==second_id)
    assert m['comparison']['relation']=='same_task_repetition' and m['comparison']['performance_improvement'] is None
    third=finished(c,g,retest_observation_id=oid);third_id=observation(c,g,third,1)
    m=next(x for x in memories(c,g) if x['id']==third_id)
    assert m['comparison']['relation']=='different_task_not_calibrated'
    post(c,base(g)+f'/written-memory/{oid}/retract',{'reason':'旧参考待复核'})
    m=next(x for x in memories(c,g) if x['id']==second_id)
    assert m['comparison']['source_active'] is False and m['retraction_reason'] is None
    assert create(c,g,retest_observation_id=oid).json()['code']=='retest_source_unavailable'


def test_withdrawal_during_new_attempt_preserves_answer_but_blocks_old_comparison(context):
    c,g=context;s=finished(c,g);oid=observation(c,g,s)
    r=create(c,g,retest_observation_id=oid);assert r.status_code==202;r=r.json()
    post(c,base(g)+f'/written-memory/{oid}/retract',{'reason':'撤回旧依据'})
    r=end(c,g,r).json();assert r['report']
    assert publish(c,g,r,r['questions'][0]['question_id']).json()['code']=='retest_source_retracted'


def test_binding_owner_role_package_and_reference_rules(context,pg_settings):
    c,g=context;a=application(c,g);s=finished(c,g,application_id=a['id']);oid=observation(c,g,s)
    assert create(c,g,retest_observation_id=oid).json()['code']=='retest_application_mismatch'
    b=application(c,g,title='另一职位')
    assert create(c,g,application_id=b['id'],retest_observation_id=oid).json()['code']=='retest_application_mismatch'
    assert create(c,g,application_package_revision=1).status_code==400
    assert create(c,g,application_id=a['id'],interview_round=' ').status_code==400
    assert create(c,g,application_id=a['id'],application_package_revision=30).status_code==404
    other=post(c,'/api/career-goals/',{'message':'另一目标'}).json()
    assert create(c,other,application_id=a['id']).status_code==404
    assert create(c,other,retest_observation_id=oid).json()['code']=='retest_source_unavailable'
    with client_for(pg_settings[0]) as outsider:
        outsider.headers['Origin']=pg_settings[0].origin;uid=signup(outsider)['user']['id']
        assert create(outsider,g,application_id=a['id']).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def test_builtin_java_preserves_binding_through_worker(written,worker_settings):
    c,g=written
    post(c,base(g)+'/messages',{'expected_version':1,'message':'补充实际岗位','jd_text':'Java SQL岗位'})
    a=application(c,g,expected_version=2)
    s=java(c,g,expected_version=2,application_id=a['id'],interview_round='Java笔试').json()
    assert s['application']['id']==a['id'];submit(c,g,s);arun(grade(worker_settings,FakeExecutor('WRONG_ANSWER')))
    s=java_end(c,g,java_view(c,g,s)).json()
    r=publish(c,g,s,s['submissions'][0]['id']);assert r.status_code==201,r.text
    app=c.get(url(g)+'/'+a['id']).json()
    assert app['written_observations'][0]['application']['interview_round']=='Java笔试'


def test_imported_practice_binding_and_reference_rejection(written,worker_settings):
    from test_programming_imports import document,body as imported_body
    from test_question_banks import imported
    c,g=written
    post(c,base(g)+'/messages',{'expected_version':1,'message':'补岗位','jd_text':'Java岗位'})
    a=application(c,g,expected_version=2);b=imported(c,document());bank={'bank_id':b['bank_id'],'version':b['version']}
    body=imported_body(bank);body['expected_version']=2
    bad=post(c,base(g)+'/written-programming-validations',{**body,'application_id':a['id']})
    assert bad.status_code==400 and bad.json()['code']=='reference_validation_cannot_bind_practice_context'
    r=post(c,base(g)+'/written-programming-validations',body);assert r.status_code==202,r.text
    arun(grade(worker_settings,FakeExecutor()))
    r=post(c,base(g)+'/written',{**body,'application_id':a['id'],'interview_round':'自定义Java'})
    assert r.status_code==202,r.text;s=r.json();submit(c,g,s);arun(grade(worker_settings,FakeExecutor()))
    s=java_end(c,g,java_view(c,g,s)).json();r=publish(c,g,s,s['submissions'][0]['id'])
    assert r.status_code==201,r.text
    assert c.get(url(g)+'/'+a['id']).json()['written_observations'][0]['application']['id']==a['id']
