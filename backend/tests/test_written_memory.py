"""Real authenticated SQL/Redis seams; executor is injected, no provider HTTP."""
from uuid import uuid4
import pytest
from test_postgres import arun,pg_settings,query,client_for,signup
from test_worker import worker_settings
from test_interview_repository import context,create as interview,process as interview_process,view as interview_view
from test_written_question_sets import create,answer,end,base,post,view
from test_written_repository import written,FakeExecutor,process,create as java,submit,view as java_view,end as java_end

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def publish(c,g,s,source,key=None):
    return post(c,base(g)+f"/written/{s['id']}/observations",{'expected_version':s['version'],'source_id':source},key)


def memories(c,g):return c.get(base(g)+'/written-memory').json()['items']


def test_source_report_idempotence_withdrawal_and_frozen_training(context,worker_settings,pg_settings):
    c,g=context;s=create(c,g).json();qid=s['questions'][0]['question_id']
    assert publish(c,g,s,qid).status_code==409
    s=answer(c,g,s,s['questions'][0],['A']).json()['session'];s=end(c,g,s).json();original=s['report']
    key=uuid4().hex;r=publish(c,g,s,qid,key);assert r.status_code==201,r.text
    oid=r.json()['observation_id'];assert publish(c,g,s,qid,key).status_code==200
    assert publish(c,g,s,s['questions'][1]['question_id'],key).status_code==409
    assert publish(c,g,s,qid).status_code==409
    assert publish(c,g,s,'absent').status_code==404
    m=memories(c,g)[0];assert m['source']['report_sha256']==original['content_sha256']
    assert m['outcome']=='does_not_match_declared_key' and not m['ability_certified']
    assert m['policy']['external_assistance_verified'] is False
    r=interview(c,g);assert r.status_code==202,r.text;t=r.json()
    frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(t['id'],))[0]['input_payload']
    assert frozen['prior_written_results'][0]['id']==oid
    arun(interview_process(worker_settings));t=interview_view(c,g,t)
    assert t['status']=='WAITING_FOR_USER'
    path=base(g)+f'/written-memory/{oid}/retract'
    assert post(c,path,{'reason':'声明答案有争议，先撤回待核验。'}).status_code==200
    assert post(c,path,{'reason':'声明答案有争议，先撤回待核验。'}).status_code==200
    assert post(c,path,{'reason':'改写撤回历史'}).status_code==409
    assert interview_view(c,g,t)['status']=='STALE'
    newer=interview(c,g).json()
    frozen2=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(newer['id'],))[0]['input_payload']
    assert frozen2['prior_written_results']==[]
    assert frozen['prior_written_results'][0]['id']==oid and view(c,g,s)['report']==original
    assert memories(c,g)[0]['retraction_reason']


def test_memory_owner_goal_and_role_scope(context,pg_settings):
    c,g=context;s=create(c,g).json();s=end(c,g,s).json();qid=s['questions'][0]['question_id']
    oid=publish(c,g,s,qid).json()['observation_id']
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert other.get(base(g)+'/written-memory').status_code==404
        assert publish(other,g,s,qid).status_code==404
        assert post(other,base(g)+f'/written-memory/{oid}/retract',{'reason':'跨用户'}).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    another=post(c,'/api/career-goals/',{'message':'另一目标'}).json()
    assert publish(c,another,s,qid).status_code==404
    t=interview(c,g,role='ai-application').json()
    frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(t['id'],))[0]['input_payload']
    assert frozen['prior_written_results']==[]


@pytest.mark.parametrize('verdict,allowed',[('ACCEPTED',True),('WRONG_ANSWER',True),('ENVIRONMENT_ERROR',False),('CANCELLED',False)])
def test_code_result_requires_actual_execution_seam_and_keeps_source(written,worker_settings,verdict,allowed):
    c,g=written;s=java(c,g).json();submit(c,g,s);arun(process(worker_settings,FakeExecutor(verdict)))
    s=java_view(c,g,s);sid=s['submissions'][0]['id']
    assert publish(c,g,s,sid).status_code==409
    s=java_end(c,g,s).json();r=publish(c,g,s,sid)
    assert r.status_code==(201 if allowed else 409),r.text
    if allowed:
        m=memories(c,g)[0]
        assert m['source']['source_sha256']==s['submissions'][0]['source_sha256']
        assert m['outcome']==verdict and m['policy']['image']


def test_model_scope_exposes_outcome_and_scope_without_answer_keys():
    from interview_agent.interviews.agent import scope_view
    scope={'policy':{},'facts':[],'current_question':None,'answer':None,'assisted_topics':[],
           'target':{},'question_history':[],'prior_observations':[],
           'prior_written_results':[dict(id='o',session_id='s',source_id='q',goal_version=1,topic_id='t',outcome='ungraded_missing_key',
                                        qualification='declared',policy={},source={'reference':'PRIVATE ANSWER'})]}
    v=scope_view(scope);assert v['prior_written_results'][0]['outcome']=='ungraded_missing_key'
    assert 'PRIVATE ANSWER' not in str(v)


def test_reference_validation_is_not_learner_evidence(written,worker_settings):
    from test_programming_imports import document,validate,create as practice
    from test_question_banks import imported
    c,g=written;b=imported(c,document());bank={'bank_id':b['bank_id'],'version':b['version']}
    s=validate(c,g,bank).json();arun(process(worker_settings,FakeExecutor()));s=java_view(c,g,s)
    assert s['validation']['passed']
    assert publish(c,g,s,s['submissions'][0]['id']).status_code==409
    s=practice(c,g,bank).json();submit(c,g,s);arun(process(worker_settings,FakeExecutor('WRONG_ANSWER')))
    s=java_end(c,g,java_view(c,g,s)).json()
    assert publish(c,g,s,s['submissions'][0]['id']).status_code==201
    m=memories(c,g)[0];assert m['source']['bank_item']['version']==bank['version']
    assert m['source']['bank_item']['item_id']=='sum-two'


def test_unknown_execution_and_cancelled_practice_do_not_publish(written,pg_settings):
    c,g=written;s=java(c,g).json();r=submit(c,g,s).json();sid=r['submission_id']
    # Injection of an already-started lost result, not an observed worker crash.
    query(pg_settings[1],"UPDATE app.career_written_submissions SET status='FAILED',execution_started=true,error_code='execution_outcome_unknown' WHERE id=%s RETURNING id",(sid,))
    s=java_end(c,g,java_view(c,g,s)).json();assert publish(c,g,s,sid).status_code==409
    s=java(c,g).json();s=java_end(c,g,s,'cancel').json()
    assert publish(c,g,s,sid).status_code==409
