"""Source-bound stop/deadline and new app/dispatcher instances; no provider HTTP."""
import pytest
from interview_agent.contracts.operations import QueueMessage
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.written.repository import WrittenRepository
from test_postgres import arun,pg_settings,query,client_for
from test_worker import worker_settings,resources
from test_career_handoffs import material_context,goal,base,post,endpoint
from test_application_coordinator import process as career_process
from test_interview_repository import process,view
from test_written_question_sets import create as written,answer as written_answer

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def start(c,g,settings,continuation=True):
    arun(career_process(settings));v=goal(c,g)
    r=post(c,endpoint(g,v['steps'][-1]['id'],'training'),{'expected_version':1,'role':'java-backend','continue_on_completion':continuation})
    assert r.status_code==202,r.text
    return r.json()


def notification(owner,s):
    r=query(owner,'SELECT id,job_id,goal_version FROM app.career_interview_runs WHERE session_id=%s ORDER BY created_at DESC LIMIT 1',(s['id'],))[0]
    return QueueMessage(operation_id=str(r['id']),job_id=str(r['job_id']),resource_version=r['goal_version'])


@pytest.mark.parametrize('before_first',[True,False])
@pytest.mark.parametrize('continuation',[True,False])
def test_stop_pauses_parent_without_model_and_fences_old_job(material_context,worker_settings,pg_settings,before_first,continuation):
    c,g=material_context;s=start(c,g,worker_settings,continuation);note=notification(pg_settings[1],s)
    if not before_first:arun(process(worker_settings));s=view(c,g,s)
    old=goal(c,g);r=post(c,base(g)+f"/interviews/{s['id']}/stop",{'expected_version':s['version']})
    assert r.status_code==200,r.text;s=r.json();v=goal(c,g)
    assert s['status']=='COMPLETED' and s['report']['stop_reason']=='user_stop'
    assert v['status']=='WAITING_FOR_USER' and v['action']['type']=='ask_user' and '停止' in v['action']['text']
    assert len(v['steps'])==len(old['steps'])==1 and v['model_requests']==old['model_requests']
    async def late():
        async with resources(worker_settings) as (old,q):return await InterviewRepository(old.pool).claim(note)
    assert arun(late()) is None
    # Recreate the ASGI app and its DB pool, not a claimed OS/process restart.
    with client_for(pg_settings[0]) as reopened:
        reopened.headers['Origin']=pg_settings[0].origin;reopened.cookies.update(c.cookies)
        assert goal(reopened,g)==v and view(reopened,g,s)['status']=='COMPLETED'
    assert goal(c,g)==v


def test_workflow_refreshes_deadline_once_without_queue_or_paid_wakeup(material_context,worker_settings,pg_settings):
    c,g=material_context;s=start(c,g,worker_settings);arun(process(worker_settings));s=view(c,g,s)
    old=goal(c,g)
    query(pg_settings[1],"UPDATE app.career_interviews SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(s['id'],))
    workflow=c.get(base(g)+'/workflow').json()
    assert workflow['interviews'][0]['status']=='COMPLETED'
    v=goal(c,g);assert v['status']=='WAITING_FOR_USER' and '到期' in v['action']['text']
    assert len(v['steps'])==1 and v['model_requests']==old['model_requests']
    assert c.get(base(g)+'/workflow').json()==workflow and goal(c,g)==v
    assert view(c,g,s)['report']['stop_reason']=='deadline'


def test_attempt_cap_restores_user_control_and_preserves_run_failure(material_context,worker_settings,pg_settings):
    c,g=material_context;s=start(c,g,worker_settings);note=notification(pg_settings[1],s)
    query(pg_settings[1],"UPDATE app.career_interview_runs SET attempt=3 WHERE id=%s RETURNING id",(note.operation_id,))
    async def reclaim():
        async with resources(worker_settings) as (old,q):return await InterviewRepository(old.pool).claim(note)
    assert arun(reclaim()) is None
    v=goal(c,g);assert v['status']=='WAITING_FOR_USER' and '恢复次数' in v['action']['text']
    assert view(c,g,s)['status']=='FAILED' and len(v['steps'])==1
    assert query(pg_settings[1],'SELECT error_code FROM app.career_interview_runs WHERE id=%s',(note.operation_id,))[0]['error_code']=='attempt_limit'
    assert arun(reclaim()) is None and goal(c,g)==v


@pytest.mark.parametrize('change',['cancel','new_version'])
def test_late_stop_never_reopens_cancelled_or_changed_goal(material_context,worker_settings,pg_settings,change):
    c,g=material_context;s=start(c,g,worker_settings)
    if change=='cancel':post(c,base(g)+'/cancel',{'expected_version':1})
    else:post(c,base(g)+'/messages',{'expected_version':1,'message':'切换目标，先不再训练'})
    before=goal(c,g)
    assert view(c,g,s)['status']=='STALE'
    assert goal(c,g)==before


def test_workflow_finalizes_idle_written_report_without_submission_or_model(material_context,pg_settings):
    c,g=material_context;r=written(c,g);assert r.status_code==202,r.text;s=r.json()
    r=written_answer(c,g,s,s['questions'][0],['B']);assert r.status_code==201;s=r.json()['session']
    query(pg_settings[1],"UPDATE app.career_written_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(s['id'],))
    before=goal(c,g);w=c.get(base(g)+'/workflow').json();result=w['written_tests'][0]
    assert result['status']=='COMPLETED' and result['declared_key_match_count']==1
    first=query(pg_settings[1],'SELECT payload,content_sha256 FROM app.career_written_reports WHERE session_id=%s',(s['id'],))[0]
    assert c.get(base(g)+'/workflow').json()==w
    assert query(pg_settings[1],'SELECT payload,content_sha256 FROM app.career_written_reports WHERE session_id=%s',(s['id'],))[0]==first
    assert goal(c,g)==before
