import asyncio
from uuid import uuid4
import httpx
import pytest
from psycopg.types.json import Jsonb
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for
from interview_agent.career.billing import CareerPolicy,CareerLedger
from interview_agent.billing.ledger import authorize,BillingUnavailable
from interview_agent.worker.queue import OutboxDispatcher,QueueTransport
from interview_agent.contracts.operations import QueueMessage
from test_postgres import arun,client_for,pg_settings,query,signup
from test_worker import worker_settings,resources

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


@pytest.fixture
def career(pg_settings,worker_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin']=pg_settings[0].origin
        user=signup(client)['user']['id']
        yield client,user
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(user,))


def create(client,**kwargs):
    return client.post('/api/career-goals/',json={'message':'我想找AI实习',**kwargs},headers={'Idempotency-Key':uuid4().hex})


def message_for(owner,goal):
    row=query(owner,'SELECT id::text,job_id::text,goal_version FROM app.career_steps WHERE goal_id=%s ORDER BY goal_version DESC LIMIT 1',(goal['id'],))[0]
    return QueueMessage(operation_id=row['id'],job_id=row['job_id'],resource_version=row['goal_version'])


async def process(settings):
    async with resources(settings) as (old,queue):
        repo=CareerRepository(old.pool)
        assert await OutboxDispatcher(repo,queue).dispatch()>=1
        assert await worker_for(repo,queue,mode='scripted').once()


def test_api_idempotent_goal_input_snapshot_and_owner(career,pg_settings):
    client,user=career; key=uuid4().hex; body={'message':'  原文\n\n保留  '}
    a=client.post('/api/career-goals/',json=body,headers={'Idempotency-Key':key}); assert a.status_code==202,a.text
    b=client.post('/api/career-goals/',json=body,headers={'Idempotency-Key':key}); assert b.status_code==200
    assert a.json()['id']==b.json()['id'] and len(b.json()['steps'])==1
    assert b.json()['steps'][0]['message']==body['message']
    assert client.post('/api/career-goals/',json={'message':'不同'},headers={'Idempotency-Key':key}).status_code==409
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin; signup(other)
        assert other.get('/api/career-goals/'+a.json()['id']).status_code==404
        assert other.post('/api/career-goals/'+a.json()['id']+'/messages',json={'message':'越权','expected_version':1},headers={'Idempotency-Key':uuid4().hex}).status_code==404


def test_scripted_worker_waits_persistently_and_new_confirmed_city_has_version(career,worker_settings):
    client,_=career; goal=create(client).json(); arun(process(worker_settings))
    view=client.get('/api/career-goals/'+goal['id']).json()
    assert view['status']=='WAITING_FOR_USER' and view['model_requests']==2
    assert view['steps'][0]['origin']=='scripted_protocol' and view['profile']['cities']==[]
    arun(asyncio.sleep(.02)); again=client.get('/api/career-goals/'+goal['id']).json()
    assert again['model_requests']==2
    reply={'message':'改为深圳，准备Java后端','expected_version':1,'conditions':{'direction':'Java后端','cities':['深圳'],'city_policy':'required'}}
    key=uuid4().hex
    assert client.post(f"/api/career-goals/{goal['id']}/messages",json=reply,headers={'Idempotency-Key':key}).status_code==202
    assert client.post(f"/api/career-goals/{goal['id']}/messages",json=reply,headers={'Idempotency-Key':key}).json()['version']==2
    async def verify_context():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool)
            async with repo.pool.connection() as conn:
                row=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s',(goal['id'],))).fetchone()
                context=await repo.context_at(conn,row)
                assert len(context.previous_actions)==1 and context.previous_actions[0].type=='ask_user'
                assert context.previous_actions[0].text==view['action']['text']
    arun(verify_context())
    arun(process(worker_settings)); result=client.get('/api/career-goals/'+goal['id']).json()
    assert result['status']=='WAITING_FOR_TOOL' and result['conditions']['cities']==['深圳']
    assert result['model_requests']==4 and len(result['steps'])==2


def test_jd_skips_exploration_and_cancel_stops_new_actions(career,worker_settings):
    client,_=career; goal=create(client,jd_text='合成职位：Java接口开发。').json(); arun(process(worker_settings))
    view=client.get('/api/career-goals/'+goal['id']).json()
    assert view['action']['type']=='prepare_training' and view['status']=='WAITING_FOR_TOOL'
    assert client.post(f"/api/career-goals/{goal['id']}/cancel",json={'expected_version':1}).json()['status']=='CANCELLED'


def test_late_claim_after_user_change_cannot_save_or_charge(career,worker_settings,pg_settings):
    client,_=career; goal=create(client).json(); notification=message_for(pg_settings[1],goal)
    async def claim():
        async with resources(worker_settings) as (old,queue): return await CareerRepository(old.pool).claim(notification)
    old_claim=arun(claim())
    assert client.post(f"/api/career-goals/{goal['id']}/messages",json={'message':'只考虑深圳','expected_version':1},headers={'Idempotency-Key':uuid4().hex}).status_code==202
    async def stale():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool)
            assert not await repo.save(old_claim,{'type':'ask_user','text':'旧问题'},'scripted_protocol')
            assert not await repo.complete(old_claim) and await repo.can_ack(notification)
    arun(stale())


def test_new_ledger_holds_unknown_separately_and_never_retries(career,worker_settings,pg_settings,project_root):
    client,_=career; goal=create(client).json(); notification=message_for(pg_settings[1],goal)
    policy=CareerPolicy.load(project_root/'configs/ja01-deepseek-flash.json'); identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity))
    async def scenario():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool); claim=await repo.claim(notification); context=await repo.context(claim)
            ledger=CareerLedger(old.pool,policy,identity)
            call=await ledger.reserve(claim,context,'a'*64,100)
            assert (await ledger.summary())['accounted_upper_nano']==policy.reservation_nano
            assert not await ledger.settle(call,error_code='synthetic-unknown')
            with pytest.raises(BillingUnavailable,match='model_usage_unknown'): await ledger.reserve(claim,context,'b'*64,100)
            assert (await ledger.summary())['http_attempts']==1
    try: arun(scenario())
    finally:
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_real_sdk_uses_career_prompt_and_wire_budget_before_candidate(career,worker_settings,pg_settings,project_root,tmp_path):
    client,_=career; goal=create(client).json(); policy=CareerPolicy.load(project_root/'configs/ja01-deepseek-flash.json')
    identity='test-'+uuid4().hex; arun(authorize(pg_settings[1],policy,identity))
    key=tmp_path/'key.env'; key.write_text('DEEPSEEK_API_KEY=offline-not-a-real-key\n')
    sent=[]
    async def scenario():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool)
            async def respond(request):
                import json
                from test_billing import reply
                from interview_agent.runtime.types import AssistantResponse,ToolCall
                payload=json.loads(await request.aread()); sent.append(payload)
                assert '求职协调' in payload['messages'][0]['content'] and 'read_current_answer' not in payload['messages'][0]['content']
                assert (await CareerLedger(old.pool,policy,identity).summary())['http_attempts']==len(sent)
                tool='read_goal' if len(sent)==1 else 'ask_user'
                args={} if tool=='read_goal' else {'text':'请确认应用还是算法方向。','fields':{}}
                result=AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=tool,arguments=args)])
                return httpx.Response(200,json=reply(result))
            await OutboxDispatcher(repo,queue).dispatch()
            await worker_for(repo,queue,mode='live',policy=policy,authorization=identity,credentials=key,
                             inner_factory=lambda:httpx.MockTransport(respond)).once()
            audit=await CareerLedger(old.pool,policy,identity).summary()
            assert audit['http_attempts']==2 and audit['usage_status']=='known'
    try:
        arun(scenario()); view=client.get('/api/career-goals/'+goal['id']).json()
        assert view['status']=='WAITING_FOR_USER' and view['steps'][0]['origin']=='real_model'
    finally:
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_insufficient_budget_is_durable_waiting_and_sends_zero_http(career,worker_settings,pg_settings,project_root,tmp_path):
    client,_=career; goal=create(client).json(); policy=CareerPolicy.load(project_root/'configs/ja01-deepseek-flash.json')
    identity='test-'+uuid4().hex; arun(authorize(pg_settings[1],policy,identity,policy.reservation_nano-1))
    key=tmp_path/'key.env'; key.write_text('DEEPSEEK_API_KEY=offline-not-a-real-key\n')
    def forbidden(request): pytest.fail('HTTP must not be sent without reservation')
    async def scenario():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool); await OutboxDispatcher(repo,queue).dispatch()
            await worker_for(repo,queue,mode='live',policy=policy,authorization=identity,credentials=key,
                inner_factory=lambda:httpx.MockTransport(forbidden)).once()
            assert (await CareerLedger(old.pool,policy,identity).summary())['http_attempts']==0
    try:
        arun(scenario()); view=client.get('/api/career-goals/'+goal['id']).json()
        assert view['status']=='WAITING_CREDENTIALS' and view['steps'][0]['error_code']=='model_budget_exhausted'
    finally: query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_cached_candidate_reclaim_commits_without_provider_and_original_is_immutable(career,worker_settings,pg_settings):
    import psycopg
    client,_=career; goal=create(client).json(); notification=message_for(pg_settings[1],goal)
    async def seed():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool); claim=await repo.claim(notification)
            assert await repo.save(claim,{'type':'ask_user','text':'缓存的澄清'},'scripted_protocol')
    arun(seed())
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],"UPDATE app.career_steps SET message='篡改' WHERE id=%s RETURNING id",(notification.operation_id,))
    query(pg_settings[1],"UPDATE app.career_steps SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(notification.operation_id,))
    async def reclaim():
        async with resources(worker_settings) as (old,queue):
            repo=CareerRepository(old.pool); await queue.publish(notification)
            await worker_for(repo,queue,mode='disabled').once()
    arun(reclaim()); view=client.get('/api/career-goals/'+goal['id']).json()
    assert view['status']=='WAITING_FOR_USER' and view['model_requests']==0


def test_new_message_never_resets_goal_request_budget(career,worker_settings,pg_settings):
    client,_=career; goal=create(client).json()
    query(pg_settings[1],'UPDATE app.career_goals SET model_requests=12,coordination_research_requests=12 WHERE id=%s RETURNING id',(goal['id'],))
    response=client.post(f"/api/career-goals/{goal['id']}/messages",json={'message':'新的输入','expected_version':1},headers={'Idempotency-Key':uuid4().hex})
    assert response.status_code==202 and response.json()['model_requests']==12
    arun(process(worker_settings)); view=client.get('/api/career-goals/'+goal['id']).json()
    assert view['status']=='FAILED' and view['model_requests']==12
