"""Real SQL/Redis continuation tests; only scripted providers and synthetic accounts."""
from uuid import uuid4
import asyncio
import pytest
from interview_agent.career.contracts import GoalContext,validate_decision
from test_career_handoffs import material_context,pg_settings,worker_settings,post,goal,base,endpoint
from test_material_repository import source,process as material_process
from test_interview_repository import process as interview_process,view,answer
from test_application_coordinator import process as career_process
from test_postgres import arun,query
from test_worker import resources
from interview_agent.materials.repository import MaterialRepository
from interview_agent.contracts.operations import QueueMessage
from interview_agent.career.handoffs import continue_after


def start(c,g,settings,kind='materials',*,continuation=True):
    if kind=='materials':assert source(c,g).status_code==201
    arun(career_process(settings));step=goal(c,g)['steps'][-1]['id']
    command={'expected_version':1,'continue_on_completion':continuation}
    command.update({'expected_revision':1} if kind=='materials' else {'role':'java-backend','max_questions':1})
    result=post(c,endpoint(g,step,kind),command)
    assert result.status_code==202,result.text
    return result.json(),command,step


async def repeat_completion(settings,gid,payload,kind,rid,status):
    async with resources(settings) as (old,q):
        async with old.pool.connection() as conn,conn.transaction():
            g=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(gid,))).fetchone()
            return await continue_after(conn,g,payload,kind,rid,status)


@pytest.mark.postgres
@pytest.mark.redis
def test_material_completion_wakes_same_goal_version_and_waits_for_fact_confirmation(material_context,worker_settings,pg_settings):
    c,g=material_context;r,command,parent=start(c,g,worker_settings)
    assert post(c,endpoint(g,parent,'materials'),{**command,'continue_on_completion':False},'different-'+uuid4().hex).status_code==409
    arun(material_process(worker_settings));v=goal(c,g)
    assert v['status']=='PENDING' and v['version']==1 and [s['cycle'] for s in v['steps']]==[0,1]
    payload=query(pg_settings[1],'SELECT input_payload FROM app.career_material_runs WHERE id=%s',(r['run_id'],))[0]['input_payload']
    assert not arun(repeat_completion(worker_settings,g['id'],payload,'materials',r['run_id'],'SUCCEEDED'))
    assert len(goal(c,g)['steps'])==2
    arun(career_process(worker_settings));v=goal(c,g)
    assert v['status']=='WAITING_FOR_USER' and v['action']['type']=='ask_user' and '确认' in v['action']['text']
    assert not c.get(base(g)+'/materials').json()['facts']
    row=query(pg_settings[1],'SELECT input_payload FROM app.career_steps WHERE id=%s',(v['steps'][-1]['id'],))[0]
    assert row['input_payload']['origin']=='tool_result'


@pytest.mark.postgres
@pytest.mark.redis
def test_interview_finish_wakes_after_last_answer_not_after_first_question(material_context,worker_settings):
    c,g=material_context;s,_,_=start(c,g,worker_settings,'training')
    arun(interview_process(worker_settings));s=view(c,g,s)
    assert len(goal(c,g)['steps'])==1 and s['status']=='WAITING_FOR_USER'
    assert answer(c,g,s,'我会说明同步边界和验证方法。').status_code==202
    arun(interview_process(worker_settings));assert view(c,g,s)['status']=='COMPLETED'
    assert goal(c,g)['status']=='PENDING' and goal(c,g)['version']==1
    arun(career_process(worker_settings));v=goal(c,g)
    assert v['action']['type']=='ask_user' and '面试已结束' in v['action']['text']
    assert len(c.get(base(g)+'/interviews').json()['items'])==1


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.parametrize('change',['cancel','reply'])
def test_late_notifications_do_not_restart_cancelled_or_changed_goals(material_context,worker_settings,pg_settings,change):
    c,g=material_context;r,_,_=start(c,g,worker_settings)
    payload=query(pg_settings[1],'SELECT input_payload FROM app.career_material_runs WHERE id=%s',(r['run_id'],))[0]['input_payload']
    if change=='cancel':post(c,base(g)+'/cancel',{'expected_version':1})
    else:post(c,base(g)+'/messages',{'expected_version':1,'message':'换一个目标，先不要继续。'})
    before=goal(c,g)
    assert not arun(repeat_completion(worker_settings,g['id'],payload,'materials',r['run_id'],'SUCCEEDED'))
    assert goal(c,g)==before


@pytest.mark.postgres
@pytest.mark.redis
def test_credential_wait_does_not_enqueue_more_model_work(material_context,worker_settings):
    c,g=material_context;start(c,g,worker_settings)
    arun(material_process(worker_settings,provider='disabled'))
    assert len(goal(c,g)['steps'])==1
    assert c.get(base(g)+'/materials').json()['runs'][0]['status']=='WAITING_CREDENTIALS'


@pytest.mark.postgres
@pytest.mark.redis
def test_failed_task_wakes_to_clarification_without_retry(material_context,worker_settings,pg_settings):
    c,g=material_context;r,_,_=start(c,g,worker_settings)
    row=query(pg_settings[1],'SELECT id,job_id,goal_version FROM app.career_material_runs WHERE id=%s',(r['run_id'],))[0]
    async def fail():
        async with resources(worker_settings) as (old,q):
            repo=MaterialRepository(old.pool)
            claim=await repo.claim(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
            return await repo.fail_assessment(claim,code='synthetic_failure')
    assert arun(fail())
    arun(career_process(worker_settings));v=goal(c,g)
    assert v['action']['type']=='ask_user' and '失败' in v['action']['text']
    assert len(c.get(base(g)+'/materials').json()['runs'])==1


def test_system_notification_cannot_be_extracted_as_user_preference():
    identity=str(uuid4())
    context=GoalContext(goal_id=str(uuid4()),user_id=str(uuid4()),version=1,conditions={},jd_text='',
                        messages=[{'id':identity,'text':'系统通知 Java 完成','origin':'tool_result'}])
    with pytest.raises(ValueError,match='ungrounded'):
        validate_decision(context,{'type':'ask_user','text':'请确认。','fields':{'direction':{'value':'Java','message_id':identity,'quote':'Java'}}})


@pytest.mark.postgres
@pytest.mark.redis
def test_wakeup_transaction_rolls_back_and_can_be_redelivered(material_context,worker_settings,pg_settings):
    c,g=material_context;r,_,_=start(c,g,worker_settings)
    payload=query(pg_settings[1],'SELECT input_payload FROM app.career_material_runs WHERE id=%s',(r['run_id'],))[0]['input_payload']
    async def rolled_back():
        async with resources(worker_settings) as (old,q):
            async with old.pool.connection() as conn,conn.transaction():
                current=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(g['id'],))).fetchone()
                assert await continue_after(conn,current,payload,'materials',r['run_id'],'SUCCEEDED')
                raise RuntimeError('synthetic_rollback')
    with pytest.raises(RuntimeError,match='synthetic_rollback'):arun(rolled_back())
    assert len(goal(c,g)['steps'])==1 and goal(c,g)['status']=='WAITING_FOR_TOOL'
    arun(material_process(worker_settings))
    assert len(goal(c,g)['steps'])==2 and goal(c,g)['status']=='PENDING'


@pytest.mark.postgres
@pytest.mark.redis
def test_cycle_limit_is_visible_and_input_cycle_is_immutable(material_context,worker_settings,pg_settings):
    from psycopg import errors
    from psycopg.types.json import Jsonb
    c,g=material_context;r,_,_=start(c,g,worker_settings)
    payload=query(pg_settings[1],'SELECT input_payload FROM app.career_material_runs WHERE id=%s',(r['run_id'],))[0]['input_payload']
    parent=query(pg_settings[1],'SELECT * FROM app.career_steps WHERE goal_id=%s',(g['id'],))[0]
    # Synthetic fixture at the maximum cycle; not a claimed real three-turn model run.
    row=query(pg_settings[1],'''INSERT INTO app.career_steps(goal_id,goal_version,cycle,request_id,request_hash,message,input_payload,status,candidate)
        VALUES(%s,1,3,%s,%s,%s,%s,'SUCCEEDED',%s) RETURNING id''',
        (g['id'],uuid4().hex,parent['request_hash'],'synthetic cycle cap',Jsonb({}),Jsonb(parent['candidate'])))[0]
    payload['coordinator_handoff']['step_id']=str(row['id'])
    assert not arun(repeat_completion(worker_settings,g['id'],payload,'materials',r['run_id'],'SUCCEEDED'))
    assert goal(c,g)['coordination_cycle']==goal(c,g)['continuation_limit']==3
    with pytest.raises(errors.CheckViolation):
        query(pg_settings[1],'UPDATE app.career_steps SET cycle=2 WHERE id=%s RETURNING id',(row['id'],))
