"""Goal-scoped application tools, bounded graph and SQL revalidation of evidence."""
import asyncio,json
from uuid import uuid4
import httpx,pytest
from interview_agent.career.contracts import GoalContext
from interview_agent.career.coordinator import coordinate,ScriptedCoordinator
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall
from interview_agent.storage.errors import ApiError
from interview_agent.worker.queue import OutboxDispatcher
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings,resources
from test_application_repository import context,create as application,url
from test_career_goals import message_for

def call(name,args=None):return AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args or {})])
def unit_context():
    aid=str(uuid4());c=GoalContext(goal_id=str(uuid4()),user_id=str(uuid4()),version=1,messages=[],conditions={},jd_text='',
        applications=[{'id':aid,'version':1,'company':'合成','title':'Java','status':'DRAFT'}])
    v={'application_id':aid,'context_sha256':'a'*64,'actions':[{'id':'b'*64,'kind':'confirm_entry','reason':'请确认申请入口。','evidence_refs':['package:1']}]}
    return c,v
def choice(v):return {'application_id':v['application_id'],'context_sha256':v['context_sha256'],'action_id':v['actions'][0]['id']}

def test_graph_reads_application_before_selecting_server_action():
    c,v=unit_context();seen=[]
    async def read(aid):seen.append(aid);return v
    p=ScriptedProvider([call('read_goal'),call('read_application',{'application_id':v['application_id']}),call('prepare_application_action',{'application':choice(v)})])
    d,trace=asyncio.run(coordinate(c,p,asyncio.Event(),read))
    assert d.text=='请确认申请入口。' and d.application_plan is None and seen==[v['application_id']]
    assert [t['status'] for t in trace]==['ok']*3 and len(p.requests)==3

@pytest.mark.parametrize('extra',[{}, {'application_id':str(uuid4())}])
def test_unread_or_other_application_cannot_be_used_but_can_ask_user(extra):
    c,v=unit_context();seen=[]
    async def forbidden(aid):seen.append(aid);raise AssertionError('out of scope reader')
    bad=call('read_application',extra) if extra else call('prepare_application_action',{'application':choice(v)})
    p=ScriptedProvider([call('read_goal'),bad,call('ask_user',{'text':'请确认要复盘的申请。'})])
    d,trace=asyncio.run(coordinate(c,p,asyncio.Event(),forbidden))
    assert d.type=='ask_user' and trace[1]['status']=='error' and not seen

@pytest.mark.parametrize('patch',[{'context_sha256':'c'*64},{'action_id':'c'*64}])
def test_forged_context_or_action_is_never_collected(patch):
    c,v=unit_context()
    async def read(aid):return v
    p=ScriptedProvider([call('read_goal'),call('read_application',{'application_id':v['application_id']}),call('prepare_application_action',{'application':{**choice(v),**patch}})])
    with pytest.raises(ValueError,match='request_limit'):asyncio.run(coordinate(c,p,asyncio.Event(),read))
    assert len(p.requests)==3

@pytest.mark.parametrize('injection',[{'text':'已经代投成功'},{'fields':{}},{'application_plan':{}}])
def test_model_cannot_supply_action_text_profile_or_server_plan(injection):
    c,v=unit_context()
    async def read(aid):return v
    p=ScriptedProvider([call('read_goal'),call('read_application',{'application_id':v['application_id']}),call('prepare_application_action',{'application':choice(v),**injection})])
    with pytest.raises(ValueError,match='request_limit'):asyncio.run(coordinate(c,p,asyncio.Event(),read))

async def process(settings,**kwargs):
    async with resources(settings) as (old,q):
        repo=CareerRepository(old.pool);await OutboxDispatcher(repo,q).dispatch()
        assert await worker_for(repo,q,mode=kwargs.pop('mode','scripted'),**kwargs).once()
def goal(c,g):return c.get('/api/career-goals/'+g['id']).json()
def post(c,p,b):return c.post(p,json=b,headers={'Idempotency-Key':uuid4().hex})
async def take(settings,note,aid):
    async with resources(settings) as (old,q):
        repo=CareerRepository(old.pool);claim=await repo.claim(note);view=await repo.application_context(claim,aid)
        return claim,{'type':'prepare_application_action','text':'此文本将由服务端行动替换','application':choice(view)}

def test_scripted_ambiguous_applications_ask_instead_of_selecting():
    c,v=unit_context();c.applications.append(c.applications[0].model_copy(update={'id':str(uuid4())}))
    async def admit():pass
    async def forbidden(aid):raise AssertionError('must clarify first')
    d,trace=asyncio.run(coordinate(c,ScriptedCoordinator(c,admit),asyncio.Event(),forbidden))
    assert d.type=='ask_user' and len(trace)==2

@pytest.mark.postgres
@pytest.mark.redis
def test_scripted_coordinator_persists_canonical_plan_without_changing_progress(context,worker_settings):
    c,g=context;a=application(c,g);arun(process(worker_settings));v=goal(c,g)
    assert v['status']=='WAITING_FOR_USER' and v['model_requests']==3 and v['application_plan_state']=='current'
    plan=v['action']['application_plan'];assert plan['execution']=='proposal_only' and plan['application_id']==a['id']
    assert v['action']['text']==plan['reason'] and plan['evidence_refs']==['package:1']
    assert [t['tool'] for t in v['steps'][0]['tool_log']]==['read_goal','read_application','prepare_application_action']
    assert c.get(url(g)+'/'+a['id']).json()['events']==[] and c.get(url(g)+'/'+a['id']).json()['status']=='DRAFT'
    assert goal(c,g)['action']==v['action'] and goal(c,g)['model_requests']==3

@pytest.mark.postgres
@pytest.mark.redis
def test_changed_evidence_is_rejected_before_save(context,worker_settings,pg_settings):
    c,g=context;a=application(c,g);note=message_for(pg_settings[1],g);claim,d=arun(take(worker_settings,note,a['id']))
    post(c,url(g)+'/'+a['id']+'/availability',{'expected_application_version':1,'availability':'closed','note':'用户报告关闭'})
    async def save():
        async with resources(worker_settings) as (old,q):return await CareerRepository(old.pool).save(claim,d,'scripted_protocol')
    assert arun(save()) is False
    v=goal(c,g);assert v['status']=='FAILED' and v['steps'][0]['error_code']=='application_evidence_changed' and v['action'] is None

@pytest.mark.postgres
@pytest.mark.redis
def test_cached_plan_is_rechecked_after_reclaim_and_get_marks_later_change(context,worker_settings,pg_settings):
    c,g=context;a=application(c,g);note=message_for(pg_settings[1],g);claim,d=arun(take(worker_settings,note,a['id']))
    async def save():
        async with resources(worker_settings) as (old,q):return await CareerRepository(old.pool).save(claim,d,'scripted_protocol')
    assert arun(save())
    post(c,url(g)+'/'+a['id']+'/availability',{'expected_application_version':1,'availability':'closed','note':'用户报告关闭'})
    query(pg_settings[1],"UPDATE app.career_steps SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(note.operation_id,))
    arun(process(worker_settings,mode='disabled'));v=goal(c,g)
    assert v['steps'][0]['error_code']=='application_evidence_changed' and v['model_requests']==0

@pytest.mark.postgres
@pytest.mark.redis
def test_read_rechecks_goal_ownership_and_cancelled_claim(context,worker_settings,pg_settings):
    c,g=context;a=application(c,g);other=post(c,'/api/career-goals/',{'message':'另一目标','jd_text':'其他岗位'}).json()
    b=application(c,other);note=message_for(pg_settings[1],g)
    async def scenario():
        async with resources(worker_settings) as (old,q):
            repo=CareerRepository(old.pool);claim=await repo.claim(note)
            with pytest.raises(ApiError) as e:await repo.application_context(claim,b['id'])
            assert e.value.status==404
            return claim
    claim=arun(scenario());c.post('/api/career-goals/'+g['id']+'/cancel',json={'expected_version':1})
    async def cancelled():
        async with resources(worker_settings) as (old,q):
            with pytest.raises(ValueError,match='claim_expired'):await CareerRepository(old.pool).application_context(claim,a['id'])
    arun(cancelled())

@pytest.mark.postgres
@pytest.mark.redis
def test_completed_observation_retraction_invalidates_visible_plan(context,worker_settings,pg_settings):
    from test_interview_repository import completed
    c,g=context;a=application(c,g);s=completed(c,g,worker_settings,application_id=a['id'])
    arun(process(worker_settings));v=goal(c,g);plan=v['action']['application_plan']
    assert plan['kind']=='retest_topic' and v['application_plan_state']=='current'
    oid=next(ref.split(':')[1] for ref in plan['evidence_refs'] if ref.startswith('observation:'))
    post(c,f"/api/career-goals/{g['id']}/interview-memory/{oid}/retract",{'reason':'用户撤回该观察'})
    changed=goal(c,g);assert changed['application_plan_state']=='outdated' and '已更新' in changed['action']['text']
    saved=query(pg_settings[1],'SELECT candidate FROM app.career_steps WHERE goal_id=%s',(g['id'],))[0]['candidate']
    assert saved['application_plan']==plan # Reading does not rewrite history.

@pytest.mark.postgres
@pytest.mark.redis
def test_sdk_mock_reads_application_and_reserves_three_requests_without_raw_resume(context,worker_settings,pg_settings,project_root,tmp_path):
    from interview_agent.career.billing import CareerPolicy
    from interview_agent.billing.ledger import authorize
    from test_interview_repository import cleanup
    from test_billing import reply
    c,g=context;canary='PRIVATE-RESUME-'+uuid4().hex
    source=post(c,f"/api/career-goals/{g['id']}/materials/sources",{'expected_version':1,'expected_revision':0,'name':'synthetic','format':'text','text':canary}).json()
    a=application(c,g,source_id=source['source_id'],expected_material_revision=1)
    identity='test-'+uuid4().hex;policy=CareerPolicy.load(project_root/'configs/ja01-deepseek-flash.json');arun(authorize(pg_settings[1],policy,identity))
    key=tmp_path/'mock.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    async def handler(request):
        payload=json.loads(request.content);seen.append(payload);assert canary not in json.dumps(payload)
        async with resources(worker_settings) as (old,q):
            async with old.pool.connection() as conn:
                count=await (await conn.execute('SELECT count(*) AS n FROM app.model_http_calls WHERE authorization_id=%s',(identity,))).fetchone()
                assert count['n']==len(seen)
        if len(seen)==1:name,args='read_goal',{}
        elif len(seen)==2:
            view=json.loads(payload['messages'][-1]['content']);assert view['applications'][0]['id']==a['id']
            name,args='read_application',{'application_id':a['id']}
        else:
            view=json.loads(payload['messages'][-1]['content']);assert view['material_kind']=='source'
            name,args='prepare_application_action',{'application':choice(view)}
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])))
    try:
        arun(process(worker_settings,mode='live',policy=policy,authorization=identity,credentials=key,inner_factory=lambda:httpx.MockTransport(handler)))
        assert len(seen)==3 and goal(c,g)['application_plan_state']=='current'
    finally:cleanup(pg_settings[1],identity)
