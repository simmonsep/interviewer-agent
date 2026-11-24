import asyncio
import json
from uuid import uuid4
import httpx
import pytest
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.worker import worker_for
from interview_agent.materials.agent import ScriptedMaterials,run_material_agent
from interview_agent.materials.billing import MaterialPolicy,MaterialLedger
from interview_agent.billing.ledger import authorize,BillingUnavailable
from interview_agent.career.budget import NON_CODING_REQUEST_ALLOCATION
from interview_agent.worker.queue import OutboxDispatcher,QueueTransport
from interview_agent.contracts.operations import QueueMessage
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_postgres import arun,client_for,pg_settings,query,signup
from test_worker import worker_settings,resources
from test_billing import reply,USAGE

pytestmark=[pytest.mark.postgres,pytest.mark.redis]
TEXT='我负责实现SQL事务和Redis通知。\n通过独立数据库验证取消后旧结果不能提交。'


@pytest.fixture
def material_context(pg_settings,worker_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin']=pg_settings[0].origin;uid=signup(client)['user']['id']
        g=client.post('/api/career-goals/',headers={'Idempotency-Key':uuid4().hex},json={'message':'准备Java后端面试','jd_text':'岗位要求Java、SQL，能解释事务与并发。','conditions':{'direction':'Java后端'}}).json()
        yield client,g
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def url(g):return f"/api/career-goals/{g['id']}/materials"
def view(client,g):return client.get(url(g)).json()
def post(client,path,body,key=None):return client.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex})
def source(client,g,text=TEXT,revision=0,key=None):return post(client,url(g)+'/sources',{'expected_version':1,'expected_revision':revision,'name':'项目.md','format':'markdown','text':text},key)
def start(client,g,revision,mode='analyze',**kwargs):return post(client,url(g)+'/runs',{'expected_version':1,'expected_revision':revision,'mode':mode,**kwargs})


async def process(settings,**kwargs):
    async with resources(settings) as (old,queue):
        repo=MaterialRepository(old.pool);await OutboxDispatcher(repo,queue).dispatch()
        assert await worker_for(repo,queue,provider=kwargs.pop('provider','scripted'),**kwargs).once()


def analyze(client,g,settings):
    response=start(client,g,1);assert response.status_code==202
    arun(process(settings));v=view(client,g);assert v['runs'][0]['status']=='SUCCEEDED'
    return v['runs'][0]


def confirm(client,g,run,key=None,revision=1):
    return post(client,url(g)+'/confirmations',{'expected_version':1,'expected_revision':revision,'run_id':run['id'],
                'proposal_ids':list(range(len(run['result']['proposals'])))},key)


def test_material_analysis_human_confirmation_draft_and_export_without_web(material_context,worker_settings):
    c,g=material_context;assert view(c,g)['material_revision']==0
    assert source(c,g).status_code==201
    r=analyze(c,g,worker_settings);assert not view(c,g)['facts'] and r['result']['action']=='analysis'
    assert confirm(c,g,r).status_code==201
    v=view(c,g);assert v['material_revision']==2 and len(v['facts'])==2
    assert all(f['verification']=='user_confirmed' for f in v['facts'])
    assert start(c,g,2,'draft').status_code==202;arun(process(worker_settings));v=view(c,g)
    draft=v['drafts'][0];assert draft['version']==1 and not draft['stale']
    assert all(f['quote'] in TEXT for f in draft['payload']['facts']) and draft['payload']['target']['kind']=='user_jd'
    exported=c.get(url(g)+f"/drafts/{draft['id']}.md")
    assert exported.status_code==200 and exported.text==draft['markdown'] and exported.headers['x-material-stale']=='false'
    assert v['model_requests']==7 and v['material_model_requests']==7
    assert c.get(f"/api/career-goals/{g['id']}").json()['version']==1


def test_idempotency_revision_conflict_and_payload_scope(material_context,worker_settings):
    c,g=material_context;key=uuid4().hex
    first=source(c,g,key=key);again=source(c,g,key=key)
    assert first.status_code==201 and again.status_code==200 and first.json()['source_id']==again.json()['source_id']
    assert source(c,g,text='不同内容',key=key).json()['code']=='idempotency_conflict'
    assert source(c,g).json()['code']=='material_revision_conflict'
    k=uuid4().hex;body={'expected_version':1,'expected_revision':1}
    a=post(c,url(g)+'/runs',body,k);b=post(c,url(g)+'/runs',body,k)
    assert a.status_code==202 and b.status_code==200 and a.json()['run_id']==b.json()['run_id']
    assert start(c,g,1).json()['code']=='material_run_in_progress'
    assert post(c,url(g)+'/runs',{**body,'user_id':str(uuid4())}).status_code==400


def test_no_resume_and_unconfirmed_draft_are_clarification_not_fabrication(material_context,worker_settings):
    c,g=material_context;assert start(c,g,0).status_code==202;arun(process(worker_settings))
    result=view(c,g)['runs'][0]['result'];assert result['action']=='ask_user' and not view(c,g)['drafts']
    assert source(c,g).status_code==201
    assert start(c,g,1,'draft').status_code==202;arun(process(worker_settings))
    assert view(c,g)['runs'][0]['result']['action']=='ask_user' and not view(c,g)['drafts']


def test_confirmation_is_explicit_idempotent_and_rejects_invented_proposal(material_context,worker_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings)
    body={'expected_version':1,'expected_revision':1,'run_id':r['id'],'proposal_ids':[11]}
    assert post(c,url(g)+'/confirmations',body).status_code==400
    key=uuid4().hex;assert confirm(c,g,r,key).status_code==201
    assert confirm(c,g,r,key).status_code==200 and view(c,g)['material_revision']==2
    assert confirm(c,g,r,revision=2).json()['code']=='material_analysis_stale'


def test_sources_runs_facts_and_exports_are_account_scoped(material_context,worker_settings,pg_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    start(c,g,2,'draft');arun(process(worker_settings));did=view(c,g)['drafts'][0]['id']
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert other.get(url(g)).status_code==404
        assert other.get(url(g)+f'/drafts/{did}.md').status_code==404
        assert source(other,g).status_code==404
        assert post(other,url(g)+'/confirmations',{'expected_version':1,'expected_revision':2,'run_id':r['id'],'proposal_ids':[0]}).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    other_goal=post(c,'/api/career-goals/',{'message':'另一目标'}).json()
    sid=view(c,g)['sources'][0]['id']
    assert start(c,other_goal,0,source_ids=[sid]).status_code==404
    assert c.get(url(other_goal)+f'/drafts/{did}.md').status_code==404


def test_material_change_fences_worker_and_old_drafts_stay_immutable(material_context,worker_settings,pg_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r);start(c,g,2,'draft');arun(process(worker_settings))
    old=view(c,g)['drafts'][0]
    run=start(c,g,2,'draft').json()['run_id'];note=notification(pg_settings[1],run)
    async def take():
        async with resources(worker_settings) as (repo,q):return await MaterialRepository(repo.pool).claim(note)
    claim=arun(take());source(c,g,text='新版本：个人贡献需要重新确认。',revision=2)
    async def expired():
        async with resources(worker_settings) as (repo,q):
            repo=MaterialRepository(repo.pool)
            assert not await repo.complete(claim) and not await repo.heartbeat(claim)
    arun(expired());v=view(c,g)
    assert v['drafts'][0]['markdown']==old['markdown'] and v['drafts'][0]['stale']
    assert v['runs'][0]['status']=='CANCELLED'
    assert c.get(url(g)+f"/drafts/{old['id']}.md").headers['x-material-stale']=='true'


def notification(owner,identity):
    r=query(owner,'SELECT job_id,goal_version FROM app.career_material_runs WHERE id=%s',(identity,))[0]
    return QueueMessage(operation_id=identity,job_id=str(r['job_id']),resource_version=r['goal_version'])


def test_cancel_before_claim_and_goal_change_stop_material_work(material_context,worker_settings,pg_settings):
    c,g=material_context;source(c,g);r=start(c,g,1).json()['run_id']
    assert c.post(url(g)+f'/runs/{r}/cancel',json={'expected_version':1,'expected_revision':1}).status_code==200
    assert view(c,g)['runs'][0]['status']=='CANCELLED' and view(c,g)['material_model_requests']==0
    r=start(c,g,1).json()['run_id']
    post(c,f"/api/career-goals/{g['id']}/messages",{'expected_version':1,'message':'改成另一岗位'})
    async def dispatch():
        async with resources(worker_settings) as (repo,q):await MaterialRepository(repo.pool).pending_dispatch()
    arun(dispatch());v=view(c,g);assert v['runs'][0]['status']=='CANCELLED' and v['runs'][0]['stale']


def test_cached_candidate_reclaim_has_zero_new_provider_calls(material_context,worker_settings,pg_settings):
    c,g=material_context;source(c,g);rid=start(c,g,1).json()['run_id'];note=notification(pg_settings[1],rid)
    async def save():
        async with resources(worker_settings) as (old,q):
            repo=MaterialRepository(old.pool);claim=await repo.claim(note);scope=await repo.context(claim)
            value=await run_material_agent(scope,ScriptedMaterials(scope,lambda:repo.admit_scripted(claim)),asyncio.Event())
            assert await repo.save(claim,value,'scripted_protocol')
    arun(save());query(pg_settings[1],"UPDATE app.career_material_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(rid,))
    arun(process(worker_settings,provider='disabled'))
    assert view(c,g)['runs'][0]['status']=='SUCCEEDED' and view(c,g)['material_model_requests']==3


def test_disabled_model_wait_is_durable_and_no_fee_calls(material_context,worker_settings):
    c,g=material_context;source(c,g);start(c,g,1);arun(process(worker_settings,provider='disabled'))
    v=view(c,g);assert v['runs'][0]['status']=='WAITING_CREDENTIALS' and v['runs'][0]['error_code']=='model_not_enabled'
    assert v['material_model_requests']==0


def test_new_material_queue_suffix_reuses_transport(worker_settings):
    r=worker_settings.redis.connect();QueueTransport(r,worker_settings.stream+':materials')


def test_real_sdk_mock_uses_new_authorization_and_settles_before_candidate(material_context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=material_context;source(c,g);start(c,g,1)
    policy=MaterialPolicy.load(project_root/'configs/ja04-deepseek-flash.json');auth='test-'+uuid4().hex;arun(authorize(pg_settings[1],policy,auth))
    key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    def respond(request):
        payload=json.loads(request.content);seen.append(payload)
        if len(seen)==1:name,args='read_material_scope',{}
        elif len(seen)==2:
            observation=json.loads(payload['messages'][-1]['content']);name,args='read_source',{'source_id':observation['sources'][0]['id']}
        else:
            observation=json.loads(payload['messages'][-1]['content']);name,args='submit_analysis',{'facts':[{'source_id':observation['id'],'kind':'contribution','quote':TEXT.splitlines()[0]}]}
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])))
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=auth,credentials=key,inner_factory=lambda:httpx.MockTransport(respond)))
        v=view(c,g);assert v['runs'][0]['status']=='SUCCEEDED' and v['runs'][0]['origin']=='real_model'
        assert v['material_model_requests']==3 and all('材料Agent' in x['messages'][0]['content'] for x in seen)
        rows=query(pg_settings[1],'SELECT state,purpose FROM app.model_http_calls WHERE authorization_id=%s',(auth,))
        assert len(rows)==3 and all(r=={'state':'KNOWN','purpose':'career_materials'} for r in rows)
    finally:cleanup_auth(pg_settings[1],auth)


def cleanup_auth(owner,auth):
    query(owner,'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(auth,))
    query(owner,'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(auth,))


@pytest.mark.parametrize('known',[False,True])
def test_paid_reclaim_does_not_repeat_without_cached_candidate(material_context,worker_settings,pg_settings,project_root,known):
    c,g=material_context;source(c,g);rid=start(c,g,1).json()['run_id'];note=notification(pg_settings[1],rid)
    policy=MaterialPolicy.load(project_root/'configs/ja04-deepseek-flash.json');auth='test-'+uuid4().hex;arun(authorize(pg_settings[1],policy,auth))
    async def scenario():
        async with resources(worker_settings) as (old,q):
            repo=MaterialRepository(old.pool);claim=await repo.claim(note);scope=await repo.context(claim);ledger=MaterialLedger(old.pool,policy,auth)
            call=await ledger.reserve(claim,scope,'a'*64,100);await ledger.settle(call,usage=USAGE if known else None,http_status=200)
            async with old.pool.connection() as conn:await conn.execute("UPDATE app.career_material_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(rid,))
            next_claim=await repo.claim(note)
            with pytest.raises(BillingUnavailable,match='material_resume_requires_new_input' if known else 'model_usage_unknown'):
                await ledger.reserve(next_claim,scope,'b'*64,100)
            assert (await ledger.summary())['http_attempts']==1
    try:arun(scenario())
    finally:cleanup_auth(pg_settings[1],auth)


def test_material_goal_budget_is_monotonic_across_new_versions(material_context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=material_context;source(c,g);start(c,g,1)
    # F07Z-120: the goal fuse is the non-coding allocation, not the old 16.
    query(pg_settings[1],'UPDATE app.career_goals SET model_requests=28,material_model_requests=%s WHERE id=%s RETURNING id',(NON_CODING_REQUEST_ALLOCATION,g['id']))
    policy=MaterialPolicy.load(project_root/'configs/ja04-deepseek-flash.json');auth='test-'+uuid4().hex;arun(authorize(pg_settings[1],policy,auth))
    key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n')
    def forbidden(r):pytest.fail('budget must precede HTTP')
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=auth,credentials=key,inner_factory=lambda:httpx.MockTransport(forbidden)))
        assert view(c,g)['runs'][0]['error_code']=='model_budget_exhausted'
        source(c,g,text='补充项目事实。',revision=1)
        assert view(c,g)['material_model_requests']==NON_CODING_REQUEST_ALLOCATION and view(c,g)['model_requests']==28
        assert not query(pg_settings[1],'SELECT id FROM app.model_http_calls WHERE authorization_id=%s',(auth,))
    finally:cleanup_auth(pg_settings[1],auth)


def test_selected_job_change_cancels_old_target_and_draft_binds_snapshot(material_context,worker_settings):
    from interview_agent.career.research_repository import ResearchRepository
    from interview_agent.career.research_worker import worker_for as source_worker
    c,g=material_context;source(c,g);start(c,g,1)
    response=post(c,f"/api/career-goals/{g['id']}/research",{'expected_version':1,'kind':'import_jd'})
    assert response.status_code==202
    async def load_jd():
        async with resources(worker_settings) as (old,q):
            repo=ResearchRepository(old.pool);worker=source_worker(repo,q)
            await OutboxDispatcher(repo,q).dispatch()
            # F07Z-53/ENG-30: `dispatch` publishes *every* pending outbox row (not just this
            # test's) into the private stream, and `once()` takes one message and returns True
            # even when it could not claim it. So "once() returned True" is not "my run ran":
            # drain until this goal's research run leaves PENDING, bounded.
            for _ in range(6):
                await worker.once()
                async with old.pool.connection() as conn:
                    row=await (await conn.execute("select status from app.career_research_runs where goal_id=%s order by created_at desc limit 1",(g['id'],))).fetchone()
                if row and row['status']!='PENDING':return
            raise AssertionError('research_run_not_processed')
    arun(load_jd());jobs=c.get(f"/api/career-goals/{g['id']}/jobs").json();snapshot=jobs['items'][0]['snapshot_id']
    selected=c.post(f"/api/career-goals/{g['id']}/selection",json={'expected_version':1,'run_id':jobs['runs'][0]['id'],'snapshot_id':snapshot,'acknowledge_unknown':True})
    assert selected.status_code==200
    async def cancel_old():
        async with resources(worker_settings) as (old,q):await MaterialRepository(old.pool).pending_dispatch()
    arun(cancel_old());assert view(c,g)['runs'][0]['status']=='CANCELLED' and view(c,g)['runs'][0]['stale']
    r=analyze(c,g,worker_settings);confirm(c,g,r);start(c,g,2,'draft');arun(process(worker_settings))
    assert view(c,g)['drafts'][0]['payload']['target']['snapshot_id']==snapshot
    assert c.get(f"/api/career-goals/{g['id']}/jobs").json()['selected']['snapshot_id']==snapshot


def test_clarification_sources_are_explicitly_combined_and_facts_remain_scoped(material_context,worker_settings):
    c,g=material_context;first=source(c,g).json()['source_id'];analyze(c,g,worker_settings)
    second=source(c,g,text='补充：测试在合成数据上运行，没有生产用户。',revision=1).json()['source_id']
    rid=start(c,g,2,source_ids=[first,second]).json()['run_id'];arun(process(worker_settings));r=view(c,g)['runs'][0]
    assert r['id']==rid and len(r['result']['proposals'])==3
    assert confirm(c,g,r,revision=2).status_code==201
    facts=view(c,g)['facts'];assert len(facts)==3
    chosen=[f['id'] for f in facts if f['source_id']==first]
    assert start(c,g,3,'draft',source_ids=[first,second],fact_ids=chosen).status_code==202;arun(process(worker_settings))
    draft=view(c,g)['drafts'][0]
    assert {f['source_id'] for f in draft['payload']['facts']}=={first} and view(c,g)['material_model_requests']==11


def test_draft_versions_append_without_overwriting_previous_artifact(material_context,worker_settings):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    start(c,g,2,'draft');arun(process(worker_settings));old=view(c,g)['drafts'][0]
    fid=view(c,g)['facts'][0]['id']
    start(c,g,2,'draft',fact_ids=[fid]);arun(process(worker_settings));v=view(c,g)
    assert [d['version'] for d in v['drafts']]==[2,1]
    assert v['drafts'][1]['id']==old['id'] and v['drafts'][1]['markdown']==old['markdown']
    assert len(v['drafts'][0]['payload']['facts'])==1 and not v['drafts'][0]['stale']


def test_immutable_material_source_and_facts_reject_sql_update(material_context,worker_settings,pg_settings):
    import psycopg
    c,g=material_context;sid=source(c,g).json()['source_id'];r=analyze(c,g,worker_settings);confirm(c,g,r)
    with pytest.raises(psycopg.errors.CheckViolation):query(pg_settings[1],'UPDATE app.career_material_sources SET body=%s WHERE id=%s RETURNING id',('changed',sid))
    fid=view(c,g)['facts'][0]['id']
    with pytest.raises(psycopg.errors.CheckViolation):query(pg_settings[1],'UPDATE app.career_material_facts SET quote=%s WHERE id=%s RETURNING id',('changed',fid))


def test_sdk_independent_reads_batch_reaches_durable_draft(material_context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=material_context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    ids=[f['id'] for f in view(c,g)['facts']];start(c,g,2,'draft')
    policy=MaterialPolicy.load(project_root/'configs/ja04-deepseek-flash.json');auth='test-'+uuid4().hex;arun(authorize(pg_settings[1],policy,auth))
    key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    def handler(request):
        payload=json.loads(request.content);seen.append(payload)
        if len(seen)==1:values=[('read_material_scope',{})]
        elif len(seen)==2:values=[('read_target',{}),('read_confirmed_facts',{})]
        else:
            assert [m['role'] for m in payload['messages'][-2:]]==['tool','tool']
            assert [m['tool_call_id'] for m in payload['messages'][-2:]]==['2-read_target','2-read_confirmed_facts']
            values=[('create_draft',{'fact_ids':ids})]
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=f'{len(seen)}-{n}',name=n,arguments=a) for n,a in values])))
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=auth,credentials=key,inner_factory=lambda:httpx.MockTransport(handler)))
        assert view(c,g)['runs'][0]['status']=='SUCCEEDED' and len(view(c,g)['drafts'])==1 and len(seen)==3
    finally:cleanup_auth(pg_settings[1],auth)
