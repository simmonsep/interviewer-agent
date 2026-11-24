"""Real authenticated API + SQL/Redis Workers, scripted providers, no paid requests."""
from uuid import uuid4
import asyncio
import pytest
from interview_agent.career.contracts import GoalContext
from interview_agent.career.coordinator import coordinate
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_material_repository import material_context,source,confirm,view as material_view,process as material_process
from test_interview_repository import process as interview_process,answer,view as interview_view
from test_application_coordinator import process as career_process
from test_postgres import arun,pg_settings,query,client_for,signup
from test_worker import worker_settings


def post(c,path,body,key=None):
    return c.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex})


def goal(c,g):return c.get('/api/career-goals/'+g['id']).json()
def base(g):return '/api/career-goals/'+g['id']
def endpoint(g,step,kind):return base(g)+f'/steps/{step}/confirm-{kind}'


def test_material_tool_is_a_proposal_and_can_read_workflow_feedback():
    c=GoalContext(goal_id=str(uuid4()),user_id=str(uuid4()),version=1,messages=[],conditions={'direction':'Java'},jd_text='',
                  workflow_feedback={'materials':{'source_count':1,'confirmed_fact_count':0}})
    p=ScriptedProvider([AssistantResponse(tool_calls=[ToolCall(id='read',name='read_goal',arguments={})]),
                        AssistantResponse(tool_calls=[ToolCall(id='material',name='prepare_materials',arguments={'text':'请确认材料分析。'})])])
    d,trace=asyncio.run(coordinate(c,p,asyncio.Event()))
    assert d.type=='prepare_materials' and len(trace)==2
    assert 'workflow_feedback' in p.requests[-1][-1].text


@pytest.mark.postgres
@pytest.mark.redis
def test_confirmed_materials_to_interview_and_valid_observations_return_to_coordinator(material_context,worker_settings,pg_settings):
    c,g=material_context;assert source(c,g).status_code==201
    arun(career_process(worker_settings));v=goal(c,g);step=v['steps'][-1]['id']
    assert v['action']['type']=='prepare_materials'
    body={'expected_version':1,'expected_revision':1,'mode':'analyze'};key=uuid4().hex
    a=post(c,endpoint(g,step,'materials'),body,key)
    assert a.status_code==202,a.text
    assert post(c,endpoint(g,step,'materials'),body,key).json()['run_id']==a.json()['run_id']
    assert post(c,endpoint(g,step.upper(),'materials'),body,key).json()['run_id']==a.json()['run_id']
    assert post(c,endpoint(g,step,'materials'),body).json()['code']=='handoff_already_confirmed'
    assert post(c,endpoint(g,step.upper(),'materials'),body).json()['code']=='handoff_already_confirmed'
    assert post(c,base(g)+'/materials/runs',body,key).json()['code']=='idempotency_conflict'
    arun(material_process(worker_settings));m=material_view(c,g);assert m['runs'][0]['status']=='SUCCEEDED' and not m['facts']
    assert confirm(c,g,m['runs'][0]).status_code==201
    facts=material_view(c,g)['facts'];assert facts
    assert post(c,base(g)+'/messages',{'expected_version':1,'message':'事实已确认，请准备Java面试。'}).status_code==202
    arun(career_process(worker_settings));v=goal(c,g);step2=v['steps'][-1]['id']
    assert v['action']['type']=='prepare_training'
    command={'expected_version':2,'role':'java-backend','max_questions':2,'fact_ids':[f['id'] for f in facts]}
    k=uuid4().hex;s=post(c,endpoint(g,step2,'training'),command,k)
    assert s.status_code==202,s.text
    s=s.json()
    assert post(c,endpoint(g,step2,'training'),command,k).json()['id']==s['id']
    assert post(c,endpoint(g,step2.upper(),'training'),command,k).json()['id']==s['id']
    assert post(c,endpoint(g,step2,'training'),command).json()['code']=='handoff_already_confirmed'
    frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(s['id'],))[0]['input_payload']
    assert frozen['coordinator_handoff']['step_id']==step2 and len(frozen['facts'])==len(facts)
    arun(interview_process(worker_settings));s=interview_view(c,g,s)
    for _ in range(2):
        assert answer(c,g,s,'我会区分个人贡献、验证步骤和未解决限制。').status_code==202
        arun(interview_process(worker_settings));s=interview_view(c,g,s)
    assert s['status']=='COMPLETED'
    feed=c.get(base(g)+'/workflow').json();assert feed['observations'] and feed['materials']['confirmed_fact_count']==len(facts)
    oid=feed['observations'][0]['id']
    assert post(c,base(g)+'/interview-memory/'+oid+'/retract',{'reason':'需要更正这项观察。'}).status_code==200
    assert oid not in {o['id'] for o in c.get(base(g)+'/workflow').json()['observations']}
    assert post(c,base(g)+'/messages',{'expected_version':2,'message':'请根据训练结果准备下一步。'}).status_code==202
    async def inspect():
        from test_worker import resources
        from interview_agent.career.repository import CareerRepository
        async with resources(worker_settings) as (old,q):
            async with old.pool.connection() as conn:
                row=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s',(g['id'],))).fetchone()
                return await CareerRepository(old.pool).context_at(conn,row)
    context=arun(inspect());assert context.workflow_feedback['interviews'][0]['status']=='COMPLETED'
    assert oid not in {o['id'] for o in context.workflow_feedback['observations']}


@pytest.mark.postgres
@pytest.mark.redis
def test_handoffs_reject_unread_stale_wrong_role_and_other_owner(material_context,worker_settings,pg_settings):
    c,g=material_context;command={'expected_version':1,'role':'java-backend','max_questions':1}
    step=g['steps'][0]['id']
    assert post(c,endpoint(g,step,'training'),command).json()['code']=='coordinator_proposal_changed'
    arun(career_process(worker_settings));assert goal(c,g)['action']['type']=='prepare_training'
    assert post(c,endpoint(g,step,'materials'),{'expected_version':1,'expected_revision':0}).json()['code']=='coordinator_proposal_changed'
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert post(other,endpoint(g,step,'training'),command).status_code==404
        assert other.get(base(g)+'/workflow').status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    assert post(c,base(g)+'/messages',{'expected_version':1,'message':'更换目标，先不要开始面试。'}).status_code==202
    assert post(c,endpoint(g,step,'training'),{**command,'expected_version':2}).json()['code']=='coordinator_proposal_changed'


@pytest.mark.postgres
@pytest.mark.redis
def test_jd_without_resume_can_confirm_once_under_concurrent_requests(material_context,worker_settings,pg_settings):
    from concurrent.futures import ThreadPoolExecutor
    c,g=material_context;arun(career_process(worker_settings));step=goal(c,g)['steps'][-1]['id']
    command={'expected_version':1,'role':'java-backend','max_questions':1}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:post(c,endpoint(g,step,'training'),command),range(2)))
    assert sorted(r.status_code for r in results)==[202,409],[r.text for r in results]
    s=next(r.json() for r in results if r.status_code==202)
    frozen=query(pg_settings[1],'SELECT input_payload FROM app.career_interviews WHERE id=%s',(s['id'],))[0]['input_payload']
    assert frozen['facts']==[] and frozen['coordinator_handoff']['step_id']==step
    arun(interview_process(worker_settings));assert interview_view(c,g,s)['status']=='WAITING_FOR_USER'


@pytest.mark.postgres
@pytest.mark.redis
def test_cancelled_goal_cannot_be_confirmed_and_creates_no_interview(material_context,worker_settings,pg_settings):
    c,g=material_context;arun(career_process(worker_settings));step=goal(c,g)['steps'][-1]['id']
    assert post(c,base(g)+'/cancel',{'expected_version':1}).status_code==200
    result=post(c,endpoint(g,step,'training'),{'expected_version':1,'role':'java-backend'})
    assert result.status_code==409
    assert query(pg_settings[1],'SELECT count(*) AS n FROM app.career_interviews WHERE goal_id=%s',(g['id'],))[0]['n']==0
