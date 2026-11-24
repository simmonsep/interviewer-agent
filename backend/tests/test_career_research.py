import json
from uuid import uuid4
import httpx
import pytest
from interview_agent.career.research_repository import ResearchRepository
from interview_agent.career.research_worker import worker_for
from interview_agent.career.research_tools import ResearchInput,ResearchBundle,collect
from interview_agent.worker.queue import OutboxDispatcher
from interview_agent.contracts.operations import QueueMessage
from test_postgres import arun,client_for,pg_settings,query,signup
from test_worker import worker_settings,resources
from test_career_research_tools import handler,FIXTURE

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


@pytest.fixture
def goal(pg_settings,worker_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin']=pg_settings[0].origin
        user=signup(client)['user']['id']
        value=client.post('/api/career-goals/',headers={'Idempotency-Key':uuid4().hex},json={
            'message':'找Java实习','jd_text':'  合成JD：Java和SQL。  ',
            'conditions':{'direction':'Java','cities':['上海'],'city_policy':'required'}}).json()
        yield client,value
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(user,))


def enqueue(client,goal,**kwargs):
    return client.post(f"/api/career-goals/{goal['id']}/research",headers={'Idempotency-Key':kwargs.pop('key',uuid4().hex)},
        json={'expected_version':1,'kind':'search','keyword':'合成',**kwargs})


def view(client,goal):return client.get(f"/api/career-goals/{goal['id']}/jobs").json()


def run_id(owner,identity):
    row=query(owner,'SELECT job_id,goal_version FROM app.career_research_runs WHERE id=%s',(identity,))[0]
    return QueueMessage(operation_id=identity,job_id=str(row['job_id']),resource_version=row['goal_version'])


async def process(settings,respond=handler):
    async with resources(settings) as (old,queue):
        repo=ResearchRepository(old.pool)
        await OutboxDispatcher(repo,queue).dispatch()
        assert await worker_for(repo,queue,inner_factory=lambda:httpx.MockTransport(respond),origin='offline_fixture').once()


def test_sql_source_flow_replay_snapshot_city_and_owner(goal,worker_settings,pg_settings):
    client,g=goal;key=uuid4().hex
    first=enqueue(client,g,key=key);assert first.status_code==202,first.text
    assert enqueue(client,g,key=key).status_code==200
    assert enqueue(client,g,key=key,keyword='changed').status_code==409
    assert enqueue(client,g).status_code==409  # one active source read per goal version
    arun(process(worker_settings));v=view(client,g)
    assert v['runs'][0]['status']=='SUCCEEDED' and v['runs'][0]['result']['included']==1
    assert len(v['items'])==2 and {x['city_check'] for x in v['items']}=={'match','conflict'}
    assert v['runs'][0]['result']['origin']=='offline_fixture'
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;user=signup(other)['user']['id']
        assert other.get(f"/api/career-goals/{g['id']}/jobs").status_code==404
        assert enqueue(other,g).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(user,))


def test_identical_content_reuses_version_changed_content_appends(goal,worker_settings,pg_settings):
    client,g=goal
    enqueue(client,g);arun(process(worker_settings))
    enqueue(client,g);arun(process(worker_settings))
    ids=lambda:query(pg_settings[1],'SELECT id FROM app.career_job_snapshots WHERE goal_id=%s',(g['id'],))
    assert len(ids())==2
    def changed(r):
        result=handler(r)
        if r.method=='GET' and r.url.params['id']=='81001':
            payload=json.loads(result.content);payload['data']['requirement']='新版合成要求：Java、SQL与Redis。'
            return httpx.Response(200,json=payload)
        return result
    enqueue(client,g);arun(process(worker_settings,changed))
    assert len(ids())==3 and len(view(client,g)['items'])==6
    assert len(query(pg_settings[1],'SELECT * FROM app.career_job_observations WHERE run_id IN (SELECT id FROM app.career_research_runs WHERE goal_id=%s)',(g['id'],)))==6


def test_new_goal_version_rejects_late_save_and_hides_old_results(goal,worker_settings,pg_settings):
    client,g=goal;run=enqueue(client,g).json()['run_id'];notification=run_id(pg_settings[1],run)
    async def take():
        async with resources(worker_settings) as (old,queue):return await ResearchRepository(old.pool).claim(notification)
    claim=arun(take())
    reply=client.post(f"/api/career-goals/{g['id']}/messages",headers={'Idempotency-Key':uuid4().hex},json={'expected_version':1,'message':'改为北京','conditions':{'direction':'Java','cities':['北京'],'city_policy':'required'}})
    assert reply.status_code==202
    async def stale():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                bundle=await collect(ResearchInput(expected_version=1,kind='search',keyword='合成'),'',c,origin='offline_fixture')
            assert not await repo.save(claim,bundle) and not await repo.complete(claim)
            assert await repo.can_ack(notification)
            await repo.pending_dispatch()
    arun(stale());v=view(client,g)
    assert v['goal_version']==2 and not v['items'] and v['runs'][0]['stale']
    assert v['runs'][0]['status']=='CANCELLED'


def test_cancelled_goal_cannot_enqueue_or_commit(goal,worker_settings,pg_settings):
    client,g=goal;run=enqueue(client,g).json()['run_id'];notification=run_id(pg_settings[1],run)
    async def take():
        async with resources(worker_settings) as (old,queue):return await ResearchRepository(old.pool).claim(notification)
    claim=arun(take());client.post(f"/api/career-goals/{g['id']}/cancel",json={'expected_version':1})
    assert enqueue(client,g).status_code==409
    async def scenario():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool)
            assert not await repo.heartbeat(claim) and not await repo.complete(claim)
    arun(scenario())


def test_source_failure_partial_and_empty_are_distinct(goal,worker_settings):
    client,g=goal
    enqueue(client,g);arun(process(worker_settings,lambda r:httpx.Response(429)))
    assert view(client,g)['runs'][0]['status']=='FAILED'
    assert view(client,g)['runs'][0]['result']['failures'][0]['status']=='rate_limited'
    def partial(r):return httpx.Response(403) if r.method=='GET' and r.url.params['id']=='81002' else handler(r)
    enqueue(client,g);arun(process(worker_settings,partial));v=view(client,g)
    assert v['runs'][0]['status']=='PARTIAL' and v['runs'][0]['result']['readable']==1
    enqueue(client,g);arun(process(worker_settings,lambda r:httpx.Response(200,json={'code':'200','data':{'items':[]}})))
    assert view(client,g)['runs'][0]['status']=='SUCCEEDED' and view(client,g)['runs'][0]['result']['readable']==0


def test_user_jd_import_has_no_network_and_original_preserved(goal,worker_settings,pg_settings):
    client,g=goal
    response=enqueue(client,g,kind='import_jd',keyword='');assert response.status_code==202
    def forbidden(r):pytest.fail('JD import must perform zero external HTTP')
    arun(process(worker_settings,forbidden));v=view(client,g);item=v['items'][0]
    assert item['source_id']=='user-jd' and item['source_url'] is None
    assert item['payload']['raw_text']==g['jd_text'] and v['runs'][0]['result']['origin']=='user_provided'
    assert query(pg_settings[1],'SELECT model_requests FROM app.career_goals WHERE id=%s',(g['id'],))[0]['model_requests']==0


def test_cached_reclaim_does_not_refetch_and_snapshots_are_immutable(goal,worker_settings,pg_settings):
    import psycopg
    client,g=goal;run=enqueue(client,g).json()['run_id'];notification=run_id(pg_settings[1],run)
    async def cache():
        async with resources(worker_settings) as (old,queue):
            repo=ResearchRepository(old.pool);claim=await repo.claim(notification)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                bundle=await collect(ResearchInput(expected_version=1,kind='search',keyword='合成'),'',c,origin='offline_fixture')
            assert await repo.save(claim,bundle)
    arun(cache())
    query(pg_settings[1],"UPDATE app.career_research_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(run,))
    def forbidden(r):pytest.fail('cached candidate must not refetch')
    arun(process(worker_settings,forbidden));assert len(view(client,g)['items'])==2
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],"UPDATE app.career_job_snapshots SET payload='{}' WHERE goal_id=%s RETURNING id",(g['id'],))
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],"UPDATE app.career_research_runs SET input_payload='{}' WHERE id=%s RETURNING id",(run,))
