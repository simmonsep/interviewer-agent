import asyncio
import json
from datetime import date
from uuid import uuid4
import httpx
import pytest
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for
from interview_agent.interviews.agent import interview,ScriptedInterviewer
from interview_agent.interviews.billing import InterviewPolicy,InterviewLedger
from interview_agent.billing.ledger import authorize,BillingUnavailable
from interview_agent.worker.queue import OutboxDispatcher
from interview_agent.contracts.operations import QueueMessage
from interview_agent.runtime.types import AssistantResponse,ToolCall
from test_postgres import arun,client_for,pg_settings,query,signup
from test_worker import worker_settings,resources
from test_billing import reply,USAGE

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


@pytest.fixture
def context(pg_settings,worker_settings):
    with client_for(pg_settings[0]) as c:
        c.headers['Origin']=pg_settings[0].origin;uid=signup(c)['user']['id']
        g=post(c,'/api/career-goals/',{'message':'准备Java后端面试，无简历也可以。','conditions':{'direction':'Java后端'}}).json()
        yield c,g
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def post(c,path,body,key=None):return c.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex})
def base(g):return f"/api/career-goals/{g['id']}"
def create(c,g,**kwargs):return post(c,base(g)+'/interviews',{'expected_version':1,'role':'java-backend','max_questions':2,**kwargs})
def view(c,g,s):return c.get(base(g)+'/interviews/'+s['id']).json()
def question(s):return next(e for e in reversed(s['events']) if e['kind']=='question')
def answer(c,g,s,text='不知道，只加volatile。',key=None):return post(c,base(g)+f"/interviews/{s['id']}/answers",{'expected_version':s['version'],'question_id':question(s)['id'],'text':text},key)


async def process(settings,**kwargs):
    async with resources(settings) as (old,q):
        repo=InterviewRepository(old.pool);await OutboxDispatcher(repo,q).dispatch()
        assert await worker_for(repo,q,provider=kwargs.pop('provider','scripted'),**kwargs).once()


def first(c,g,settings,**kwargs):
    response=create(c,g,**kwargs);assert response.status_code==202
    s=response.json();arun(process(settings));s=view(c,g,s);assert s['status']=='WAITING_FOR_USER'
    return s


def completed(c,g,settings,**kwargs):
    s=first(c,g,settings,**kwargs)
    for text in ('不知道，只加volatile。','我会把检查与更新作为一个原子操作，并说明验证边界。'):
        assert answer(c,g,s,text).status_code==202;arun(process(settings));s=view(c,g,s)
    assert s['status']=='COMPLETED';return s


def test_whole_interview_waits_follows_up_and_finishes_without_web(context,worker_settings):
    c,g=context;s=first(c,g,worker_settings)
    assert question(s)['payload']['kind']=='base' and s['observations']==[] and s['report'] is None
    assert answer(c,g,s).status_code==202;arun(process(worker_settings));s=view(c,g,s)
    assert question(s)['payload']['kind']=='probe' and not s['observations']
    assert c.get(base(g)+'/interview-memory').json()['items']==[]
    assert answer(c,g,s,'复合更新需要原子性。').status_code==202;arun(process(worker_settings));s=view(c,g,s)
    assert s['status']=='COMPLETED' and s['report']['stop_reason']=='question_limit'
    assert s['report']['questions']==s['report']['answers']==2 and s['report']['assisted_answers']==0
    assert len(s['observations'])==2 and not s['report']['semantic_scoring_eligible'] and not s['report']['unreviewed_answer_ids']
    assert all(o['evidence_source']['url'].startswith('https://') and o['evidence_source']['origin']=='authored_paraphrase' for o in s['observations'])


def test_coaching_explanation_and_retest_are_marked_assisted(context,worker_settings):
    c,g=context;s=completed(c,g,worker_settings,mode='coaching')
    explanation=next(e for e in s['events'] if e['kind']=='explanation');qs=[e for e in s['events'] if e['kind']=='question']
    assert explanation['payload']['evidence_quote'] and qs[1]['payload']['kind']=='retest' and qs[1]['payload']['assisted']
    assert s['report']['assisted_answers']==1 and s['observations'][1]['assisted']
    assert s['policy']['mode']=='coaching'


def test_original_answer_idempotency_old_question_and_refresh(context,worker_settings):
    c,g=context;s=first(c,g,worker_settings);key=uuid4().hex
    a=answer(c,g,s,'  不知道。\n 保留空白。',key);b=answer(c,g,s,'  不知道。\n 保留空白。',key)
    assert a.status_code==b.status_code==202
    assert answer(c,g,s,'不同内容',key).json()['code']=='idempotency_conflict'
    arun(process(worker_settings));new=view(c,g,s)
    assert [e['payload']['text'] for e in new['events'] if e['kind']=='answer']==['  不知道。\n 保留空白。']
    assert answer(c,g,s).status_code==409
    assert c.get(base(g)+'/interviews').json()['items'][0]['id']==s['id']
    assert view(c,g,new)['events']==new['events']


def test_create_idempotency_policy_and_one_active_session(context,worker_settings):
    c,g=context;key=uuid4().hex;body={'expected_version':1,'role':'ai-application','mode':'interview','max_questions':2}
    a=post(c,base(g)+'/interviews',body,key);b=post(c,base(g)+'/interviews',body,key)
    assert a.status_code==202 and b.status_code==200 and a.json()['id']==b.json()['id']
    assert create(c,g).json()['code']=='interview_in_progress'
    assert post(c,base(g)+'/interviews',{**body,'mode':'coaching'},key).json()['code']=='idempotency_conflict'
    assert post(c,base(g)+'/interviews',{**body,'user_id':str(uuid4())}).status_code==400
    arun(process(worker_settings));assert question(view(c,g,a.json()))['payload']['topic_id']=='agent-tools'


def test_memory_is_evidence_with_correction_not_permanent_ability(context,worker_settings):
    c,g=context;s=completed(c,g,worker_settings);m=c.get(base(g)+'/interview-memory').json()['items'];assert len(m)==2
    oid=m[0]['id'];key=uuid4().hex;path=base(g)+f'/interview-memory/{oid}/retract'
    assert post(c,path,{'reason':'这条观察引用了回答，但我认为结论需要更正。'},key).status_code==200
    assert post(c,path,{'reason':'这条观察引用了回答，但我认为结论需要更正。'},key).status_code==200
    assert next(o for o in view(c,g,s)['observations'] if o['id']==oid)['correction']
    # Retest provenance is a coaching concept: a formal interview no longer freezes cross-session
    # evidence (S02b mode contract).
    new=create(c,g,mode='coaching').json()
    async def inspect():
        async with resources(worker_settings) as (old,q):
            async with old.pool.connection() as conn:return await (await conn.execute('SELECT input_payload FROM app.career_interviews WHERE id=%s',(new['id'],))).fetchone()
    prior=arun(inspect())['input_payload']['prior_observations']
    assert prior and oid not in {o['id'] for o in prior} and all(o['origin']=='prior_observation_to_retest' for o in prior)
    assert all(o['source_session_id']==s['id'] and o['answer_id'] and o['answer_quote'] and o['pack_version'] for o in prior)


def test_memory_correction_invalidates_an_inflight_session(context,worker_settings):
    c,g=context;completed(c,g,worker_settings);oid=c.get(base(g)+'/interview-memory').json()['items'][0]['id']
    s=create(c,g).json();post(c,base(g)+f'/interview-memory/{oid}/retract',{'reason':'撤回旧观察，重新训练。'})
    s=view(c,g,s);assert s['status']=='STALE' and s['report']['stop_reason']=='context_changed'


def test_user_stop_cancels_pending_work_without_extra_model_calls(context,worker_settings,pg_settings):
    c,g=context;s=first(c,g,worker_settings);answer(c,g,s)
    s=view(c,g,s);count=query(pg_settings[1],'SELECT model_requests FROM app.career_goals WHERE id=%s',(g['id'],))[0]['model_requests']
    stopped=c.post(base(g)+f"/interviews/{s['id']}/stop",json={'expected_version':s['version']}).json()
    assert stopped['status']=='COMPLETED' and stopped['report']['stop_reason']=='user_stop' and len(stopped['report']['unreviewed_answer_ids'])==1
    assert query(pg_settings[1],'SELECT model_requests FROM app.career_goals WHERE id=%s',(g['id'],))[0]['model_requests']==count
    assert stopped['run']['status']=='CANCELLED'


def test_server_deadline_finishes_waiting_session(context,worker_settings,pg_settings):
    c,g=context;s=first(c,g,worker_settings)
    query(pg_settings[1],"UPDATE app.career_interviews SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(s['id'],))
    ended=view(c,g,s);assert ended['status']=='COMPLETED' and ended['report']['stop_reason']=='deadline'
    assert answer(c,g,s).status_code==409


def test_goal_change_fences_old_work_and_preserves_history(context,worker_settings,pg_settings):
    c,g=context;s=create(c,g).json();note=notification(pg_settings[1],s['id'])
    async def claim():
        async with resources(worker_settings) as (old,q):return await InterviewRepository(old.pool).claim(note)
    claim=arun(claim());post(c,base(g)+'/messages',{'expected_version':1,'message':'改为另一个岗位。'})
    async def stale():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool);assert not await repo.heartbeat(claim) and not await repo.complete(claim)
    arun(stale());assert view(c,g,s)['status']=='STALE'


def test_accounts_cannot_read_answer_stop_or_correct_other_users(context,worker_settings,pg_settings):
    c,g=context;s=completed(c,g,worker_settings);oid=s['observations'][0]['id']
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert other.get(base(g)+f"/interviews/{s['id']}").status_code==404
        assert other.get(base(g)+'/interview-memory').status_code==404
        assert other.post(base(g)+f"/interviews/{s['id']}/stop",json={'expected_version':s['version']}).status_code==404
        assert post(other,base(g)+f'/interview-memory/{oid}/retract',{'reason':'越权'}).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def test_optional_confirmed_material_drives_project_question_and_stales_on_change(context,worker_settings):
    from test_material_repository import source,analyze,confirm
    c,g=context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r)
    materials=c.get(base(g)+'/materials').json();fid=materials['facts'][0]['id']
    s=first(c,g,worker_settings,fact_ids=[fid]);q=question(s)
    assert q['payload']['kind']=='project' and materials['facts'][0]['quote'] in q['payload']['text']
    source(c,g,text='更正：原贡献描述需要重新核对。',revision=2)
    assert view(c,g,s)['status']=='STALE'
    assert c.get(base(g)).json()['version']==1


def test_no_material_session_does_not_stale_on_unrelated_material_upload(context,worker_settings):
    from test_material_repository import source
    c,g=context;s=first(c,g,worker_settings);source(c,g)
    assert view(c,g,s)['status']=='WAITING_FOR_USER'


def notification(owner,sid):
    r=query(owner,'SELECT id,job_id,goal_version FROM app.career_interview_runs WHERE session_id=%s ORDER BY session_version DESC LIMIT 1',(sid,))[0]
    return QueueMessage(operation_id=str(r['id']),job_id=str(r['job_id']),resource_version=r['goal_version'])


def test_candidate_reclaim_skips_model_and_does_not_duplicate_question(context,worker_settings,pg_settings):
    c,g=context;s=create(c,g).json();note=notification(pg_settings[1],s['id'])
    async def save():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool);claim=await repo.claim(note);scope=await repo.context(claim)
            d=await interview(scope,ScriptedInterviewer(scope,lambda:repo.admit_scripted(claim)),asyncio.Event());await repo.save(claim,d,'scripted_protocol')
    arun(save());query(pg_settings[1],"UPDATE app.career_interview_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(note.operation_id,))
    arun(process(worker_settings,provider='disabled'));s=view(c,g,s)
    assert s['status']=='WAITING_FOR_USER' and len(s['events'])==1 and s['run']['model_requests']==3


def test_disabled_model_waits_durably_and_can_be_stopped(context,worker_settings):
    c,g=context;s=create(c,g).json();arun(process(worker_settings,provider='disabled'));s=view(c,g,s)
    assert s['status']=='WAITING_CREDENTIALS' and s['run']['model_requests']==0
    assert c.post(base(g)+f"/interviews/{s['id']}/stop",json={'expected_version':s['version']}).json()['status']=='COMPLETED'


def cleanup(owner,identity):
    query(owner,'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
    query(owner,'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))


def test_sdk_admission_then_private_sources_then_durable_question(context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=context;s=create(c,g).json();policy=InterviewPolicy.load(project_root/'configs/ja05-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity));key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    def handler(request):
        payload=json.loads(request.content);seen.append(payload)
        if len(seen)==1:name,args='read_interview_scope',{}
        elif len(seen)==2:
            scope=json.loads(payload['messages'][-1]['content']);assert 'pack' not in scope and scope['answer'] is None
            name,args='read_topics',{'topic_ids':['java-concurrency']}
        else:name,args='submit_interview_action',{'action':'ask_question','topic_id':'java-concurrency','question_id':'java-concurrency-base'}
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])))
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=identity,credentials=key,inner_factory=lambda:httpx.MockTransport(handler)))
        s=view(c,g,s);assert s['status']=='WAITING_FOR_USER' and s['run']['origin']=='real_model'
        assert query(pg_settings[1],'SELECT count(*) AS n FROM app.model_http_calls WHERE authorization_id=%s AND purpose=%s AND state=%s',(identity,'career_interview','KNOWN'))[0]['n']==3
        assert 'rubric' not in json.dumps(s) and 'reference' not in json.dumps(s['events'])
    finally:cleanup(pg_settings[1],identity)


@pytest.mark.parametrize('known',[False,True])
def test_paid_reclaim_without_candidate_is_not_automatically_rebilled(context,worker_settings,pg_settings,project_root,known):
    c,g=context;s=create(c,g).json();note=notification(pg_settings[1],s['id']);policy=InterviewPolicy.load(project_root/'configs/ja05-deepseek-flash.json');identity='test-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity))
    async def scenario():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool);claim=await repo.claim(note);scope=await repo.context(claim);ledger=InterviewLedger(old.pool,policy,identity)
            call=await ledger.reserve(claim,scope,'a'*64,100);await ledger.settle(call,usage=USAGE if known else None,http_status=200)
            async with old.pool.connection() as conn:await conn.execute("UPDATE app.career_interview_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(note.operation_id,))
            new=await repo.claim(note)
            with pytest.raises(BillingUnavailable,match='interview_resume_requires_new_input' if known else 'model_usage_unknown'):await ledger.reserve(new,scope,'b'*64,100)
            assert (await ledger.summary())['http_attempts']==1
    try:arun(scenario())
    finally:cleanup(pg_settings[1],identity)


def test_lifetime_training_budget_precedes_http_and_does_not_reset(context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=context;s=create(c,g).json();query(pg_settings[1],'UPDATE app.career_goals SET model_requests=72,training_model_requests=80 WHERE id=%s RETURNING id',(g['id'],))
    policy=InterviewPolicy.load(project_root/'configs/ja05-deepseek-flash.json');identity='test-'+uuid4().hex;arun(authorize(pg_settings[1],policy,identity))
    key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n')
    def forbidden(r):pytest.fail('no HTTP after cumulative training budget')
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=identity,credentials=key,inner_factory=lambda:httpx.MockTransport(forbidden)))
        s=view(c,g,s);assert s['status']=='WAITING_CREDENTIALS' and s['run']['error_code']=='model_budget_exhausted'
        assert not query(pg_settings[1],'SELECT id FROM app.model_http_calls WHERE authorization_id=%s',(identity,))
    finally:cleanup(pg_settings[1],identity)


def test_question_answer_and_observation_rows_cannot_be_overwritten(context,worker_settings,pg_settings):
    import psycopg
    c,g=context;s=completed(c,g,worker_settings)
    for table,identity in [('career_interview_events',s['events'][0]['id']),('career_interview_observations',s['observations'][0]['id'])]:
        with pytest.raises(psycopg.errors.CheckViolation):query(pg_settings[1],f'UPDATE app.{table} SET payload=%s WHERE id=%s RETURNING id',('{}',identity))


def test_concurrent_answers_accept_only_one_original_submission(context,worker_settings):
    from concurrent.futures import ThreadPoolExecutor
    c,g=context;s=first(c,g,worker_settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(lambda text:answer(c,g,s,text),['原回答A。','原回答B。']))
    assert sorted(r.status_code for r in replies)==[202,409]
    s=view(c,g,s);assert len([e for e in s['events'] if e['kind']=='answer'])==1


def test_draft_binding_and_stale_material_are_explicit(context,worker_settings):
    from test_material_repository import source,analyze,confirm,start,process as material_process
    c,g=context;source(c,g);r=analyze(c,g,worker_settings);confirm(c,g,r);start(c,g,2,'draft');arun(material_process(worker_settings))
    draft=c.get(base(g)+'/materials').json()['drafts'][0]
    s=first(c,g,worker_settings,draft_id=draft['id']);assert s['draft_id']==draft['id'] and question(s)['payload']['kind']=='project'
    source(c,g,text='更新后的材料。',revision=2)
    assert create(c,g,draft_id=draft['id']).json()['code']=='draft_stale'


def test_training_does_not_starve_legacy_and_material_phase_budgets(context,worker_settings,pg_settings):
    from test_career_delegation import coordinate
    from test_material_repository import source,analyze
    c,g=context
    query(pg_settings[1],'UPDATE app.career_goals SET model_requests=25,training_model_requests=25 WHERE id=%s RETURNING id',(g['id'],))
    arun(coordinate(worker_settings));source(c,g);analyze(c,g,worker_settings)
    counts=query(pg_settings[1],'SELECT model_requests,coordination_research_requests,material_model_requests,training_model_requests FROM app.career_goals WHERE id=%s',(g['id'],))[0]
    assert counts=={'model_requests':30,'coordination_research_requests':2,'material_model_requests':3,'training_model_requests':25}


def test_practice_mistakes_are_asked_but_never_scored(context,worker_settings):
    """F07Z-59: the mistake enters the session, the label stays inconclusive."""
    c,g=context
    created=c.post(base(g)+'/practice-records',
                   json={'request_id':uuid4().hex,'site_id':'leetcode',
                         'url':'https://leetcode.cn/problemset/','title':'两数之和',
                         'practiced_on':date.today().isoformat(),'outcome':'PARTIAL',
                         'note':'边界忘了判空。',
                         'statement':'给定整数数组 nums 和目标值 target，返回和为 target 的两个下标。'},
                   headers={'Idempotency-Key':uuid4().hex})
    assert created.status_code==201
    record_id=created.json()['record_id']
    s=first(c,g,worker_settings,mode='coaching',include_practice_mistakes=True)
    payload=question(s)['payload']
    assert payload['origin']=='practice_record' and payload['has_reference'] is False
    assert payload['practice_record_id']==record_id
    assert s['policy']['include_practice_mistakes'] is True
    assert 'coverage' not in s['policy']
    assert answer(c,g,s,'我用哈希表存值到下标的映射。').status_code==202
    arun(process(worker_settings));s=view(c,g,s)
    assert s['observations'][0]['label']=='inconclusive'
    assert '练习记录' in s['observations'][0]['summary']
    # F07Z-60: the coaching walkthrough is readable from the practice record afterwards.
    item=c.get(base(g)+'/practice-records').json()['items'][0]
    assert item['explanation']['text']=='（脚本流程演示：不生成讲解内容。）'
    assert item['explanation']['origin']=='scripted_protocol'
    assert item['explanation']['created_at']


def test_sdk_answer_round_admits_before_http_and_commits_exact_observation(context,worker_settings,pg_settings,project_root,tmp_path):
    c,g=context;s=first(c,g,worker_settings);answer(c,g,s);note=notification(pg_settings[1],s['id'])
    scope=query(pg_settings[1],'SELECT input_payload FROM app.career_interview_runs WHERE id=%s',(note.operation_id,))[0]['input_payload']
    async def noop():pass
    choice=ScriptedInterviewer(scope,noop).decision
    policy=InterviewPolicy.load(project_root/'configs/ja05-deepseek-flash.json');identity='test-'+uuid4().hex;arun(authorize(pg_settings[1],policy,identity))
    key=tmp_path/'key.env';key.write_text('DEEPSEEK_API_KEY=offline-not-real\n');seen=[]
    async def handler(request):
        payload=json.loads(request.content);seen.append(payload)
        async with resources(worker_settings) as (old,q):
            assert (await InterviewLedger(old.pool,policy,identity).summary())['http_attempts']==len(seen)
        if len(seen)==1:name,args='read_interview_scope',{}
        elif len(seen)==2:
            current=json.loads(payload['messages'][-1]['content']);assert current['answer']['id']==scope['answer']['id']
            name,args='read_topics',{'topic_ids':[scope['current_question']['topic_id']]}
        else:name,args='submit_interview_action',choice.model_dump(exclude={'tool_log'})
        return httpx.Response(200,json=reply(AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args)])))
    try:
        arun(process(worker_settings,provider='live',policy=policy,authorization=identity,credentials=key,inner_factory=lambda:httpx.MockTransport(handler)))
        s=view(c,g,s);assert s['status']=='WAITING_FOR_USER' and question(s)['payload']['kind']=='probe'
        row=query(pg_settings[1],'SELECT payload FROM app.career_interview_observations WHERE session_id=%s',(s['id'],))[0]
        assert row['payload']['answer_quote']==scope['answer']['text'] and row['payload']['origin']=='real_model'
    finally:cleanup(pg_settings[1],identity)


def test_a_failed_round_persists_stage_and_termination_reason(context,worker_settings,pg_settings):
    """S06A: the diagnostic must be readable after the fact, not only in memory."""
    c,g=context;s=create(c,g).json();note=notification(pg_settings[1],s['id'])
    from interview_agent.interviews.agent import InterviewFailure
    class Invalid:
        def __init__(self):self.n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:name,args='read_interview_scope',{}
            elif self.n==2:name,args='read_topics',{'topic_ids':['java-concurrency']}
            else:name,args='submit_interview_action',{'action':'ask_question','topic_id':'java-concurrency','question_id':'not-registered'}
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    async def scenario():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool);claim=await repo.claim(note);scope=await repo.context(claim)
            with pytest.raises(InterviewFailure) as caught:
                await interview(scope,Invalid(),asyncio.Event())
            await repo.fail_assessment(claim,code=caught.value.code,diagnostic=caught.value.diagnostic)
    arun(scenario())
    detail=view(c,g,s)
    assert detail['status']=='FAILED' and detail['run']['error_code']=='interview_model_limit'
    diagnostic=detail['run']['diagnostic']
    assert diagnostic['stage']=='model_round'
    assert diagnostic['termination_reason']=='model_request_limit'
    assert diagnostic['model_rounds']==5
    assert diagnostic['progress']['rounds']==5 and diagnostic['progress']['candidate_produced'] is False


def test_business_commit_without_ack_reclaims_without_a_second_model_call(context,worker_settings):
    """S06C: the answer is committed, the ACK is lost, and the next delivery must not pay again."""
    c,g=context;s=create(c,g).json()
    async def scenario():
        async with resources(worker_settings) as (old,q):
            repo=InterviewRepository(old.pool)
            await OutboxDispatcher(repo,q).dispatch()
            first=worker_for(repo,q,provider='scripted')
            original=q.ack
            async def lost(message_id):raise RuntimeError('ack_lost')
            q.ack=lost
            with pytest.raises(RuntimeError,match='ack_lost'):
                await first.once()
            q.ack=original
            pending=(await q.redis.xpending(q.stream,q.group))['pending']
            # The run is already terminal (complete ran before the lost ACK); the next delivery
            # must acknowledge it without claiming it again.
            second=worker_for(repo,q,provider='disabled');second.reclaim_idle_ms=0
            assert await second.once()
            after=(await q.redis.xpending(q.stream,q.group))['pending']
            return pending,after
    pending,after=arun(scenario())
    assert pending==1 and after==0
    detail=view(c,g,s)
    assert detail['status']=='WAITING_FOR_USER' and len(detail['events'])==1
    assert detail['run']['model_requests']==3
