"""Controlled intent and real multi-role paths. Sources/model are explicitly scripted."""
import asyncio
from uuid import uuid4
import pytest
from interview_agent.career.contracts import GoalContext,GoalInput,GoalReply,request_body
from interview_agent.career.coordinator import ScriptedCoordinator,coordinate
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_career_handoffs import material_context,worker_settings,pg_settings,post,goal,base,endpoint
from test_material_repository import source,analyze,confirm,process as material_process,view as materials
from test_interview_repository import process as interview_process,view,answer
from test_application_coordinator import process as career_process
from test_career_delegation import investigate,jobs
from test_postgres import arun


@pytest.mark.parametrize('intent,action',[('research','prepare_job_research'),('materials','prepare_materials'),('training','prepare_training'),('review','ask_user'),('finish','finish')])
def test_explicit_intent_controls_action_even_with_jd(intent,action):
    c=GoalContext(goal_id=str(uuid4()),user_id=str(uuid4()),version=1,conditions={'direction':'Java'},jd_text='JD数据',messages=[],intent=intent)
    async def admit():pass
    d,_=asyncio.run(coordinate(c,ScriptedCoordinator(c,admit),asyncio.Event()))
    assert d.type==action


def test_wrong_action_gets_feedback_and_corrects_within_existing_budget():
    c=GoalContext(goal_id=str(uuid4()),user_id=str(uuid4()),version=1,conditions={'direction':'Java'},jd_text='有JD',messages=[],intent='research')
    def call(name):return AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments={} if name=='read_goal' else {'text':'提议'})])
    p=ScriptedProvider([call('read_goal'),call('prepare_training'),call('prepare_job_research')])
    d,trace=asyncio.run(coordinate(c,p,asyncio.Event()))
    assert d.type=='prepare_job_research' and trace[1]['status']=='error' and len(p.requests)==3
    assert 'explicit_intent_conflict' in p.requests[-1][-1].text


def test_default_fields_do_not_change_old_request_identity():
    assert set(request_body(GoalInput(message='原请求')))=={'message','jd_text','conditions'}
    assert set(request_body(GoalReply(message='回复',expected_version=1)))=={'message','expected_version','conditions','jd_text'}


@pytest.mark.postgres
@pytest.mark.redis
def test_research_selection_training_review_without_resume_or_new_goal_version(material_context,worker_settings):
    c,_=material_context
    arun(career_process(worker_settings))  # Finish this fixture's initial unrelated coordinator job.
    g=post(c,'/api/career-goals/',{'message':'寻找Java岗位','continue_after_research':True,
        'conditions':{'direction':'Java','auto_research':True}}).json()
    arun(career_process(worker_settings));arun(investigate(worker_settings))
    assert goal(c,g)['status']=='PENDING'
    arun(career_process(worker_settings));assert '研究结果已保存' in goal(c,g)['action']['text']
    j=jobs(c,g);run=j['runs'][0];match=next(m for m in run['result']['matches'] if m['outcome']!='excluded')
    body={'expected_version':1,'run_id':run['id'],'snapshot_id':match['snapshot_id'],'acknowledge_unknown':True,'continue_on_selection':True}
    assert post(c,base(g)+'/selection',body).status_code==200
    assert post(c,base(g)+'/selection',body).status_code==200
    assert len(goal(c,g)['steps'])==3
    arun(career_process(worker_settings));v=goal(c,g);assert v['action']['type']=='prepare_training'
    s=post(c,endpoint(g,v['steps'][-1]['id'],'training'),{'expected_version':1,'role':'java-backend','max_questions':1,'continue_on_completion':True}).json()
    arun(interview_process(worker_settings));s=view(c,g,s)
    assert answer(c,g,s,'我会说明事务边界和验证限制。').status_code==202
    arun(interview_process(worker_settings));arun(career_process(worker_settings))
    v=goal(c,g);assert v['version']==1 and v['coordination_cycle']==3 and v['action']['type']=='ask_user'
    assert len(jobs(c,g)['runs'])==1 and c.get(base(g)+'/workflow').json()['selected_job']['snapshot_id']==match['snapshot_id']


@pytest.mark.postgres
@pytest.mark.redis
def test_fact_confirmation_continues_without_reentering_user_text(material_context,worker_settings):
    c,g=material_context;source(c,g);arun(career_process(worker_settings));step=goal(c,g)['steps'][-1]['id']
    r=post(c,endpoint(g,step,'materials'),{'expected_version':1,'expected_revision':1,'continue_on_completion':True}).json()
    arun(material_process(worker_settings));arun(career_process(worker_settings))
    body={'expected_version':1,'expected_revision':1,'run_id':r['run_id'],'proposal_ids':[0,1],'continue_after_confirmation':True}
    key=uuid4().hex
    assert post(c,base(g)+'/materials/confirmations',body,key).status_code==201
    assert post(c,base(g)+'/materials/confirmations',body,key).status_code==200
    arun(career_process(worker_settings));v=goal(c,g)
    assert v['version']==1 and v['coordination_cycle']==2 and v['action']['type']=='prepare_training'


@pytest.mark.postgres
@pytest.mark.redis
def test_new_source_is_not_treated_as_already_confirmed(material_context,worker_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    source(c,g,text='新项目：这是尚未确认的贡献。',revision=2)
    arun(career_process(worker_settings));assert goal(c,g)['action']['type']=='prepare_materials'
    feed=c.get(base(g)+'/workflow').json()['materials']
    assert feed['confirmed_fact_count']>0 and feed['latest_source_confirmed_fact_count']==0


@pytest.mark.postgres
@pytest.mark.redis
def test_explicit_finish_prevents_a_new_training_despite_jd(material_context,worker_settings):
    c,g=material_context
    post(c,base(g)+'/messages',{'expected_version':1,'message':'本轮结束','intent':'finish'})
    arun(career_process(worker_settings));assert goal(c,g)['status']=='COMPLETED'
    assert c.get(base(g)+'/interviews').json()['items']==[]
