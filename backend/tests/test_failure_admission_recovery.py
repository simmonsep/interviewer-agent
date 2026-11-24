"""Real SQL/Redis; explicit state/error injection, not real model outages."""
import pytest
from interview_agent.materials.repository import MaterialRepository
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.career.research_repository import ResearchRepository
from test_postgres import arun,pg_settings,query
from test_worker import worker_settings,resources
from test_career_continuations import material_context,start
from test_career_delegation import delegated,coordinate,jobs,notification as research_note
from test_material_repository import notification as material_note
from test_career_stop_recovery import notification as interview_note
from test_career_handoffs import goal,base,post
from interview_agent.billing.ledger import authorize,BillingUnavailable
from interview_agent.materials.billing import MaterialPolicy,MaterialLedger
from interview_agent.interviews.billing import InterviewPolicy,InterviewLedger
from interview_agent.career.billing import CareerPolicy
from interview_agent.career.delegation_billing import ResearchLedger
from uuid import uuid4

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def setup(request,kind,worker_settings,owner,*,continuation=True):
    if kind=='research':
        c,g=request.getfixturevalue('delegated');arun(coordinate(worker_settings));r=jobs(c,g)['runs'][0]
        return c,g,ResearchRepository,research_note(owner,r['id']),'career_research_runs'
    c,g=request.getfixturevalue('material_context')
    if kind=='materials':
        r,_,_=start(c,g,worker_settings,'materials',continuation=continuation)
        return c,g,MaterialRepository,material_note(owner,r['run_id']),'career_material_runs'
    r,_,_=start(c,g,worker_settings,'training',continuation=continuation)
    return c,g,InterviewRepository,interview_note(owner,r),'career_interview_runs'


@pytest.mark.parametrize('kind',['materials','research','interview'])
@pytest.mark.parametrize('failure',['cap','admission','failed'])
def test_terminal_or_admission_wait_returns_parent_without_extra_work(request,worker_settings,pg_settings,kind,failure):
    c,g,repo_type,note,table=setup(request,kind,worker_settings,pg_settings[1],continuation=failure!='failed');before=goal(c,g)
    assert before['status']=='WAITING_FOR_TOOL'
    if failure=='cap':query(pg_settings[1],f'UPDATE app.{table} SET attempt=3 WHERE id=%s RETURNING id',(note.operation_id,))
    async def stop():
        async with resources(worker_settings) as (old,q):
            repo=repo_type(old.pool);claim=await repo.claim(note)
            if failure=='cap':assert claim is None
            else:
                assert claim
                # Simulates the catch-path's persisted admission error; no real HTTP.
                assert await repo.fail_assessment(claim,waiting=failure=='admission',code='model_usage_unknown' if failure=='admission' else 'synthetic_role_failed')
            assert await repo.claim(note) is None
            assert await repo.can_ack(note)
    arun(stop());after=goal(c,g)
    assert after['status']=='WAITING_FOR_USER' and after['action']['type']=='ask_user'
    assert len(after['steps'])==len(before['steps']) and after['model_requests']==before['model_requests']
    row=query(pg_settings[1],f'SELECT status,error_code FROM app.{table} WHERE id=%s',(note.operation_id,))[0]
    assert row['error_code']=={'cap':'attempt_limit','admission':'model_usage_unknown','failed':'synthetic_role_failed'}[failure]
    assert row['status'] in ('FAILED','CANCELLED') if failure!='admission' else row['status']=='WAITING_CREDENTIALS'
    assert goal(c,g)==after


@pytest.mark.parametrize('kind',['materials','research'])
@pytest.mark.parametrize('change',['cancel','new_message'])
def test_late_failure_does_not_reopen_changed_parent(request,worker_settings,pg_settings,kind,change):
    c,g,repo_type,note,table=setup(request,kind,worker_settings,pg_settings[1])
    async def claim():
        async with resources(worker_settings) as (old,q):return await repo_type(old.pool).claim(note)
    old=arun(claim());assert old
    if change=='cancel':post(c,base(g)+'/cancel',{'expected_version':1})
    else:post(c,base(g)+'/messages',{'expected_version':1,'message':'暂停旧任务，改下一步安排'})
    before=goal(c,g)
    async def late():
        async with resources(worker_settings) as (old_repo,q):
            assert not await repo_type(old_repo.pool).fail_assessment(old,waiting=True,code='model_usage_unknown')
    arun(late());assert goal(c,g)==before


@pytest.mark.parametrize('kind',['materials','research','interview'])
def test_unsettled_reservation_blocks_reclaimed_attempt_before_http(request,worker_settings,pg_settings,project_root,kind):
    c,g,repo_type,note,table=setup(request,kind,worker_settings,pg_settings[1])
    policy_type,ledger_type,file={
        'materials':(MaterialPolicy,MaterialLedger,'ja04-deepseek-flash.json'),
        'research':(CareerPolicy,ResearchLedger,'ja03-deepseek-flash.json'),
        'interview':(InterviewPolicy,InterviewLedger,'ja05-deepseek-flash.json')}[kind]
    policy=policy_type.load(project_root/'configs'/file);identity='synthetic-recovery-'+uuid4().hex
    arun(authorize(pg_settings[1],policy,identity))
    async def scenario():
        async with resources(worker_settings) as (old,q):
            repo=repo_type(old.pool);first=await repo.claim(note)
            scope=await (repo.agent_scope(first) if kind=='research' else repo.context(first))
            ledger=ledger_type(old.pool,policy,identity)
            call=await ledger.reserve(first,scope,'a'*64,100)
            # No HTTP issued and no settlement: models the ambiguous accounting state.
            async with old.pool.connection() as conn:
                await conn.execute(f"UPDATE app.{table} SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(note.operation_id,))
            second=await repo.claim(note);assert second and second.attempt==first.attempt+1
            with pytest.raises(BillingUnavailable,match='model_usage_unknown'):
                await ledger.reserve(second,scope,'b'*64,100)
            async with old.pool.connection() as conn:
                row=await (await conn.execute('SELECT state,accounted_nano FROM app.model_http_calls WHERE id=%s',(call,))).fetchone()
            assert row['state']=='RESERVED' and row['accounted_nano'] is None
            assert (await ledger.summary())['http_attempts']==1
    try:arun(scenario())
    finally:
        # Only this synthetic authorization, never the retained historical UNKNOWN rows.
        query(pg_settings[1],'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',(identity,))
        query(pg_settings[1],'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',(identity,))
