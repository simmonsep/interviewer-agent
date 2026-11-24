import json
from pathlib import Path
from uuid import uuid4
import httpx
import pytest
from interview_agent.career.repository import CareerRepository
from interview_agent.career.research_repository import ResearchRepository
from interview_agent.career.worker import worker_for as coordinator_worker
from interview_agent.career.research_worker import worker_for as research_worker,SourceTransport
from interview_agent.career.billing import CareerPolicy
from interview_agent.career.delegation_billing import ResearchLedger
from interview_agent.billing.ledger import authorize,BillingUnavailable
from interview_agent.worker.queue import OutboxDispatcher
from interview_agent.contracts.operations import QueueMessage
from test_postgres import arun,client_for,pg_settings,query,signup
from test_worker import worker_settings,resources
from test_career_research_tools import handler
from test_billing import reply,USAGE
from interview_agent.runtime.types import AssistantResponse,ToolCall

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


@pytest.fixture
def delegated(pg_settings,worker_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin']=pg_settings[0].origin;user=signup(client)['user']['id']
        goal=client.post('/api/career-goals/',json={'message':'按确认条件研究Java实习，不需要简历。',
            'conditions':{'direction':'Java','cities':['上海'],'city_policy':'required','auto_research':True,
                          'recruitment':'intern','graduation_year':2027,'days_per_week':4,'duration_months':3,'skills':['Java','SQL']}},
            headers={'Idempotency-Key':uuid4().hex}).json()
        yield client,goal
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(user,))


def jobs(client,goal):return client.get(f"/api/career-goals/{goal['id']}/jobs").json()


def notification(owner,identity):
    row=query(owner,'SELECT job_id,goal_version FROM app.career_research_runs WHERE id=%s',(identity,))[0]
    return QueueMessage(operation_id=identity,job_id=str(row['job_id']),resource_version=row['goal_version'])


async def coordinate(settings,**kwargs):
    async with resources(settings) as (old,queue):
        repo=CareerRepository(old.pool);await OutboxDispatcher(repo,queue).dispatch()
        assert await coordinator_worker(repo,queue,mode=kwargs.pop('mode','scripted'),**kwargs).once()


async def investigate(settings,**kwargs):
    async with resources(settings) as (old,queue):
        repo=ResearchRepository(old.pool);await OutboxDispatcher(repo,queue).dispatch()
        assert await research_worker(repo,queue,provider=kwargs.pop('provider','scripted'),inner_factory=lambda:httpx.MockTransport(handler),origin='offline_fixture',**kwargs).once()


def test_transactional_delegation_independent_scope_and_evidence_selection(delegated,worker_settings,pg_settings):
    client,g=delegated;arun(coordinate(worker_settings));v=jobs(client,g)
    assert len(v['runs'])==1 and v['runs'][0]['mode']=='agent' and v['runs'][0]['parent_step_id']
    payload=query(pg_settings[1],'SELECT input_payload FROM app.career_research_runs WHERE id=%s',(v['runs'][0]['id'],))[0]['input_payload']
    scope=ResearchRepository.scope(payload,1)
    assert not {'jd_text','messages','user_id','skills'}.intersection(scope)
    assert 'skills' not in scope['conditions'] and payload['jd_text']==''
    arun(investigate(worker_settings));v=jobs(client,g);run=v['runs'][0]
    assert run['status']=='SUCCEEDED' and run['agent_origin']=='scripted_protocol'
    assert run['model_requests']==4 and run['source_requests']==3
    count=query(pg_settings[1],'SELECT model_requests,source_requests,status FROM app.career_goals WHERE id=%s',(g['id'],))[0]
    assert count=={'model_requests':6,'source_requests':3,'status':'WAITING_FOR_USER'}
    assert len(run['result']['matches'])==2
    allowed=next(x for x in run['result']['matches'] if x['outcome']!='excluded')
    excluded=next(x for x in run['result']['matches'] if x['outcome']=='excluded')
    url=f"/api/career-goals/{g['id']}/selection"
    body={'expected_version':1,'run_id':run['id'],'snapshot_id':allowed['snapshot_id']}
    assert client.post(url,json=body).status_code==409
    assert client.post(url,json={**body,'acknowledge_unknown':True}).status_code==200
    assert client.post(url,json={**body,'acknowledge_unknown':True}).status_code==200
    assert client.post(url,json={**body,'snapshot_id':excluded['snapshot_id'],'acknowledge_unknown':True}).json()['code']=='hard_requirement_conflict'
    assert jobs(client,g)['selected']['snapshot_id']==allowed['snapshot_id']


def test_same_parent_commits_one_child_and_public_api_cannot_set_agent_mode(delegated,worker_settings,pg_settings):
    client,g=delegated;arun(coordinate(worker_settings))
    parent=query(pg_settings[1],'SELECT id,job_id,goal_version FROM app.career_steps WHERE goal_id=%s',(g['id'],))[0]
    async def repeat():
        async with resources(worker_settings) as (old,queue):
            await queue.publish(QueueMessage(operation_id=str(parent['id']),job_id=str(parent['job_id']),resource_version=parent['goal_version']))
            assert await coordinator_worker(CareerRepository(old.pool),queue,mode='scripted').once()
    arun(repeat());assert len(jobs(client,g)['runs'])==1
    assert client.post(f"/api/career-goals/{g['id']}/research",headers={'Idempotency-Key':uuid4().hex},json={'expected_version':1,'kind':'search','keyword':'后台','mode':'agent','parent_step_id':str(parent['id'])}).status_code==400


def test_manual_query_does_not_race_pending_automatic_delegation(delegated,worker_settings):
    client,g=delegated
    response=client.post(f"/api/career-goals/{g['id']}/research",headers={'Idempotency-Key':uuid4().hex},json={'expected_version':1,'kind':'search','keyword':'后台'})
    assert response.status_code==409 and response.json()['code']=='coordination_in_progress'
    arun(coordinate(worker_settings));assert len(jobs(client,g)['runs'])==1


def test_goal_change_or_cancel_blocks_old_expert_source_and_commit(delegated,worker_settings,pg_settings):
    client,g=delegated;arun(coordinate(worker_settings));run=jobs(client,g)['runs'][0];note=notification(pg_settings[1],run['id'])
    async def take():
        async with resources(worker_settings) as (old,queue):return await ResearchRepository(old.pool).claim(note)
    claim=arun(take())
    client.post(f"/api/career-goals/{g['id']}/messages",headers={'Idempotency-Key':uuid4().hex},json={'expected_version':1,'message':'改为北京'})
    async def stale():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool)
            with pytest.raises(BillingUnavailable,match='execution_expired'):await repo.admit_source(claim)
            assert not await repo.complete(claim)
            await repo.pending_dispatch()
    arun(stale());v=jobs(client,g)
    assert not v['items'] and v['runs'][0]['status']=='CANCELLED' and v['runs'][0]['stale']


def test_source_budget_is_checked_before_external_transport(delegated,worker_settings,pg_settings):
    client,g=delegated;arun(coordinate(worker_settings))
    query(pg_settings[1],'UPDATE app.career_goals SET source_requests=12 WHERE id=%s RETURNING id',(g['id'],))
    sent=[]
    async def scenario():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool);await OutboxDispatcher(repo,queue).dispatch()
            def forbidden(r):sent.append(r);pytest.fail('source quota must precede HTTP')
            await research_worker(repo,queue,provider='scripted',inner_factory=lambda:httpx.MockTransport(forbidden),origin='offline_fixture').once()
    arun(scenario());run=jobs(client,g)['runs'][0]
    assert not sent and run['status']=='WAITING_CREDENTIALS' and run['error_code']=='source_budget_exhausted'
    assert run['model_requests']==2


def test_real_sdk_roles_share_authorization_and_goal_counter(delegated,worker_settings,pg_settings,project_root,tmp_path):
    client,g=delegated;policy=CareerPolicy.load(project_root/'configs/ja03-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity));key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n')
    parent_calls=[];expert_calls=[]
    def response(name,args=None):return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args or {})])))
    def parent(r):
        payload=json.loads(r.content);parent_calls.append(payload)
        return response('read_goal') if len(parent_calls)==1 else response('prepare_job_research',{'text':'按确认条件研究岗位。','fields':{}})
    async def expert(r):
        payload=json.loads(r.content);expert_calls.append(payload)
        async with resources(worker_settings) as (old,queue):
            assert (await ResearchLedger(old.pool,policy,identity).summary())['http_attempts']==2+len(expert_calls)
        assert '岗位研究Agent' in payload['messages'][0]['content']
        assert 'read_goal' not in {x['function']['name'] for x in payload['tools']}
        step=len(expert_calls)
        return response('read_research_scope') if step==1 else response('search_jobs',{'keyword':'后台'}) if step==2 else response('read_job',{'job_id':'81001'}) if step==3 else response('finish_research')
    try:
        arun(coordinate(worker_settings,mode='live',policy=policy,authorization=identity,credentials=key,inner_factory=lambda:httpx.MockTransport(parent)))
        arun(investigate(worker_settings,provider='live',policy=policy,authorization=identity,credentials=key,model_inner_factory=lambda:httpx.MockTransport(expert)))
        run=jobs(client,g)['runs'][0];assert run['status']=='SUCCEEDED' and run['agent_origin']=='real_model'
        rows=query(pg_settings[1],'SELECT purpose,count(*) AS n FROM app.model_http_calls WHERE authorization_id=%s GROUP BY purpose',(identity,))
        assert {r['purpose']:r['n'] for r in rows}=={'career_coordination':2,'career_research':4}
        assert query(pg_settings[1],'SELECT model_requests FROM app.career_goals WHERE id=%s',(g['id'],))[0]['model_requests']==6
    finally:
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_shared_goal_budget_cannot_restart_on_child_or_new_input(delegated,worker_settings,pg_settings,project_root,tmp_path):
    client,g=delegated;arun(coordinate(worker_settings));policy=CareerPolicy.load(project_root/'configs/ja03-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity));key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n')
    query(pg_settings[1],'UPDATE app.career_goals SET model_requests=12,coordination_research_requests=12 WHERE id=%s RETURNING id',(g['id'],))
    def forbidden(r):pytest.fail('no model HTTP after shared goal limit')
    try:
        arun(investigate(worker_settings,provider='live',policy=policy,authorization=identity,credentials=key,model_inner_factory=lambda:httpx.MockTransport(forbidden)))
        assert jobs(client,g)['runs'][0]['error_code']=='model_budget_exhausted'
        response=client.post(f"/api/career-goals/{g['id']}/messages",headers={'Idempotency-Key':uuid4().hex},json={'expected_version':1,'message':'继续'})
        assert response.json()['model_requests']==12
    finally:query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_selection_other_goal_is_404_and_new_version_does_not_inherit_selection(delegated,worker_settings,pg_settings):
    client,g=delegated;arun(coordinate(worker_settings));arun(investigate(worker_settings));v=jobs(client,g);r=v['runs'][0]
    m=next(x for x in r['result']['matches'] if x['outcome']!='excluded')
    body={'expected_version':1,'run_id':r['id'],'snapshot_id':m['snapshot_id'],'acknowledge_unknown':True}
    other=client.post('/api/career-goals/',headers={'Idempotency-Key':uuid4().hex},json={'message':'另一个目标'}).json()
    assert client.post(f"/api/career-goals/{other['id']}/selection",json=body).status_code==404
    with client_for(pg_settings[0]) as outsider:
        outsider.headers['Origin']=pg_settings[0].origin;uid=signup(outsider)['user']['id']
        assert outsider.post(f"/api/career-goals/{g['id']}/selection",json=body).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    assert client.post(f"/api/career-goals/{g['id']}/selection",json=body).status_code==200
    client.post(f"/api/career-goals/{g['id']}/messages",headers={'Idempotency-Key':uuid4().hex},json={'expected_version':1,'message':'更新条件'})
    assert jobs(client,g)['selected'] is None and not jobs(client,g)['items']


@pytest.mark.parametrize('known',[False,True])
def test_research_call_history_survives_reclaim_and_blocks_new_paid_request(delegated,worker_settings,pg_settings,project_root,known):
    client,g=delegated;arun(coordinate(worker_settings));identity='test-'+uuid4().hex
    policy=CareerPolicy.load(project_root/'configs/ja03-deepseek-flash.json');arun(authorize(pg_settings[1],policy,identity))
    note=notification(pg_settings[1],jobs(client,g)['runs'][0]['id'])
    async def scenario():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool);claim=await repo.claim(note);scope=await repo.agent_scope(claim)
            ledger=ResearchLedger(old.pool,policy,identity);call=await ledger.reserve(claim,scope,'a'*64,100)
            await ledger.settle(call,usage=USAGE if known else None,http_status=200)
            async with old.pool.connection() as conn:await conn.execute("UPDATE app.career_research_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(note.operation_id,))
            next_claim=await repo.claim(note)
            assert next_claim.attempt==claim.attempt+1
            with pytest.raises(BillingUnavailable,match='career_resume_requires_new_input' if known else 'model_usage_unknown'):
                await ledger.reserve(next_claim,scope,'b'*64,100)
            summary=await ledger.summary();assert summary['http_attempts']==1
            if not known:assert summary['accounted_upper_nano']==policy.reservation_nano
    try:arun(scenario())
    finally:
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_cached_expert_reclaim_skips_provider_and_source(delegated,worker_settings,pg_settings):
    import asyncio
    from interview_agent.career.research_agent import research,ScriptedResearcher
    client,g=delegated;arun(coordinate(worker_settings));note=notification(pg_settings[1],jobs(client,g)['runs'][0]['id'])
    async def cache():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool);claim=await repo.claim(note);scope=await repo.agent_scope(claim)
            async with httpx.AsyncClient(transport=SourceTransport(httpx.MockTransport(handler),repo,claim)) as http:
                bundle=await research(scope,ScriptedResearcher(lambda:repo.admit_scripted(claim)),http,asyncio.Event(),origin='offline_fixture')
            assert await repo.save(claim,bundle,'scripted_protocol')
    arun(cache())
    query(pg_settings[1],"UPDATE app.career_research_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(note.operation_id,))
    def forbidden(r):pytest.fail('cached expert must not refetch')
    async def recover():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool);await queue.publish(note)
            await research_worker(repo,queue,provider='disabled',inner_factory=lambda:httpx.MockTransport(forbidden),origin='offline_fixture').once()
    arun(recover());run=jobs(client,g)['runs'][0]
    assert run['status']=='SUCCEEDED' and run['model_requests']==4 and run['source_requests']==3
