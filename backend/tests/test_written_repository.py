import asyncio
from dataclasses import replace
from uuid import uuid4
from types import SimpleNamespace
import pytest
from interview_agent.written.repository import WrittenRepository
from interview_agent.written.worker import worker_for
from interview_agent.worker.queue import OutboxDispatcher
from interview_agent.contracts.operations import QueueMessage
from test_postgres import arun,client_for,pg_settings,query,signup
from test_worker import worker_settings,resources

pytestmark=[pytest.mark.postgres,pytest.mark.redis]
IMAGE='eclipse-temurin@sha256:'+'a'*64
SOURCE='public class Main { public static void main(String[] args) { System.out.println("PASS"); } }\n'


@pytest.fixture
def written(pg_settings,worker_settings):
    settings=pg_settings[0].model_copy(update={'written_execution_image':IMAGE})
    with client_for(settings) as c:
        c.headers['Origin']=settings.origin; uid=signup(c)['user']['id']
        g=post(c,'/api/career-goals/',{'message':'准备Java后端实习，无简历。','conditions':{'direction':'Java后端'}}).json()
        yield c,g
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


def post(c,path,body,key=None): return c.post(path,json=body,headers={'Idempotency-Key':key or uuid4().hex})
def base(g): return f"/api/career-goals/{g['id']}"
def create(c,g,**kw): return post(c,base(g)+'/written',{'expected_version':1,**kw})
def view(c,g,s): return c.get(base(g)+'/written/'+s['id']).json()
def submit(c,g,s,source=SOURCE,key=None): return post(c,base(g)+f"/written/{s['id']}/submissions",{'expected_version':s['version'],'source':source},key)
def end(c,g,s,action='finish'): return post(c,base(g)+f"/written/{s['id']}/{action}",{'expected_version':s['version']})


class FakeExecutor:
    """Injected execution seam only; not a Docker/isolation acceptance result."""
    profile=SimpleNamespace(image=IMAGE)
    def __init__(self,verdict='ACCEPTED'): self.verdict=verdict; self.calls=0
    async def grade(self,source,cases,lost):
        import hashlib
        self.calls+=1
        return {'verdict':self.verdict,'cases':[{'id':c['id'],'visibility':c['visibility'],'verdict':self.verdict} for c in cases],
                'evidence':{'image':IMAGE,'policy':'ja06-docker-java-v1','source_sha256':hashlib.sha256(source.encode()).hexdigest()}}


async def process(settings,executor=None):
    async with resources(settings) as (old,q):
        repo=WrittenRepository(old.pool); await OutboxDispatcher(repo,q).dispatch()
        assert await worker_for(repo,q,executor).once()


def notification(owner,sid):
    r=query(owner,'SELECT id,job_id,goal_version FROM app.career_written_submissions WHERE session_id=%s ORDER BY seq DESC LIMIT 1',(sid,))[0]
    return QueueMessage(operation_id=str(r['id']),job_id=str(r['job_id']),resource_version=r['goal_version'])


def test_written_roundtrip_preserves_original_code_and_hides_hidden_results_until_finish(written,worker_settings):
    c,g=written; s=create(c,g).json(); assert s['status']=='ACTIVE' and s['report'] is None
    catalog=c.get(base(g)+'/written-catalog').json(); assert catalog['execution_enabled'] and len(catalog['items'])==2
    assert 'hidden-1' not in str(catalog) and 'analysis' not in str(catalog)
    assert submit(c,g,s).status_code==202
    fake=FakeExecutor('WRONG_ANSWER'); arun(process(worker_settings,fake)); s=view(c,g,s)
    assert fake.calls==1 and s['submissions'][0]['source']==SOURCE and s['submissions'][0]['result']['execution_error'] is None
    assert 'hidden-1' not in str(s) and s['report'] is None
    s=end(c,g,s).json(); assert s['status']=='COMPLETED' and s['report']['graded_submissions']==1
    assert s['submissions'][0]['result']['verdict']=='WRONG_ANSWER' and 'hidden-1' in str(s)
    assert not s['report']['ability_certified'] and s['policy']['system_ai_help'] is False


def test_disabled_api_does_not_enqueue_or_fall_back_to_host(written,pg_settings):
    c,g=written
    with client_for(pg_settings[0]) as other:
        other.cookies.update(c.cookies); other.headers['Origin']=pg_settings[0].origin
        r=create(other,g); assert r.status_code==503 and r.json()['code']=='written_executor_disabled'
    assert c.get(base(g)+'/written').json()['items']==[]


def test_create_and_submit_are_idempotent_with_conflicting_body_rejected(written,worker_settings):
    c,g=written; key=uuid4().hex; body={'expected_version':1,'task_id':'idempotency-record'}
    a=post(c,base(g)+'/written',body,key); b=post(c,base(g)+'/written',body,key)
    assert a.status_code==202 and b.status_code==200 and a.json()['id']==b.json()['id']
    assert post(c,base(g)+'/written',{**body,'task_id':'lock-owner-expiry'},key).status_code==409
    s=a.json(); key=uuid4().hex; x=submit(c,g,s,key=key); y=submit(c,g,s,key=key)
    assert x.status_code==202 and y.status_code==200 and x.json()['submission_id']==y.json()['submission_id']
    assert submit(c,g,s,'class Main {}',key).json()['code']=='idempotency_conflict'
    assert len(view(c,g,s)['submissions'])==1


def test_one_pending_submission_and_concurrent_version_lock(written,worker_settings):
    from concurrent.futures import ThreadPoolExecutor
    c,g=written;s=create(c,g).json()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:submit(c,g,s),range(2)))
    assert sorted(r.status_code for r in results)==[202,409]
    new=view(c,g,s); assert submit(c,g,new).json()['code']=='written_execution_in_progress'
    assert create(c,g).json()['code']=='written_in_progress'


def test_finish_allows_already_accepted_submission_to_complete_and_refuses_new_code(written,worker_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s);s=view(c,g,s);s=end(c,g,s).json()
    assert s['report']['pending_submissions']==1 and submit(c,g,s).status_code==409
    arun(process(worker_settings,FakeExecutor()));s=view(c,g,s)
    assert s['submissions'][0]['result']['verdict']=='ACCEPTED' and s['report']['pending_submissions']==0


def test_cancel_fences_inflight_execution_and_rejects_late_result(written,worker_settings,pg_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s);note=notification(pg_settings[1],s['id'])
    async def claim():
        async with resources(worker_settings) as (old,q): return await WrittenRepository(old.pool).claim(note)
    claimed=arun(claim()); s=view(c,g,s); s=end(c,g,s,'cancel').json()
    assert s['status']=='CANCELLED' and s['submissions'][0]['status']=='CANCELLED'
    async def stale():
        async with resources(worker_settings) as (old,q):
            repo=WrittenRepository(old.pool)
            assert not await repo.heartbeat(claimed)
            assert not await repo.save(claimed,{'verdict':'ENVIRONMENT_ERROR','cases':[]})
    arun(stale())


def test_server_deadline_blocks_new_submission_but_grades_on_time_original(written,worker_settings,pg_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s)
    query(pg_settings[1],"UPDATE app.career_written_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(s['id'],))
    s=view(c,g,s);assert s['status']=='COMPLETED' and s['stop_reason']=='deadline' and submit(c,g,s).status_code==409
    arun(process(worker_settings,FakeExecutor()));assert view(c,g,s)['submissions'][0]['status']=='SUCCEEDED'


def test_goal_update_cancels_old_submissions_and_preserves_code(written,worker_settings,pg_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s)
    query(pg_settings[1],"UPDATE app.career_goals SET version=version+1 WHERE id=%s RETURNING id",(g['id'],))
    s=view(c,g,s);assert s['status']=='STALE' and s['submissions'][0]['source']==SOURCE and s['submissions'][0]['status']=='CANCELLED'


def test_cross_account_and_extra_fields_cannot_change_ownership_policy_or_commands(written,pg_settings):
    c,g=written;s=create(c,g).json()
    with client_for(pg_settings[0].model_copy(update={'written_execution_image':IMAGE})) as other:
        other.headers['Origin']=pg_settings[0].origin; uid=signup(other)['user']['id']
        assert other.get(base(g)+'/written/'+s['id']).status_code==404
        assert create(other,g).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    assert post(c,base(g)+f"/written/{s['id']}/submissions",{'expected_version':s['version'],'source':SOURCE,'network':True}).status_code==400


def test_original_code_and_task_payload_are_immutable_in_sql(written,pg_settings):
    import psycopg
    c,g=written;s=create(c,g).json();submit(c,g,s)
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],"UPDATE app.career_written_submissions SET source='changed' WHERE session_id=%s RETURNING id",(s['id'],))
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],"UPDATE app.career_written_sessions SET input_payload='{}'::jsonb WHERE id=%s RETURNING id",(s['id'],))


def test_disabled_worker_returns_environment_failure_without_user_wrong_answer(written,worker_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s);arun(process(worker_settings));s=view(c,g,s);s=end(c,g,s).json()
    r=s['submissions'][0];assert r['status']=='FAILED' and r['result']['verdict']=='ENVIRONMENT_ERROR'
    assert r['error_code']=='written_executor_disabled' and s['report']['graded_submissions']==0


def test_cached_result_reclaim_does_not_execute_again(written,worker_settings,pg_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s);note=notification(pg_settings[1],s['id']);fake=FakeExecutor()
    async def cache():
        async with resources(worker_settings) as (old,q):
            repo=WrittenRepository(old.pool);claim=await repo.claim(note);row=await repo.context(claim)
            assert await repo.begin(claim);result=await fake.grade(row['source'],row['input_payload']['task']['cases'],asyncio.Event())
            assert await repo.save(claim,result)
    arun(cache());query(pg_settings[1],"UPDATE app.career_written_submissions SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(note.operation_id,))
    arun(process(worker_settings,fake));assert fake.calls==1 and view(c,g,s)['submissions'][0]['status']=='SUCCEEDED'


def test_started_without_cached_result_is_not_silently_executed_twice(written,worker_settings,pg_settings):
    c,g=written;s=create(c,g).json();submit(c,g,s);note=notification(pg_settings[1],s['id']);fake=FakeExecutor()
    async def start():
        async with resources(worker_settings) as (old,q):
            repo=WrittenRepository(old.pool);claim=await repo.claim(note);assert await repo.begin(claim)
    arun(start());query(pg_settings[1],"UPDATE app.career_written_submissions SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id",(note.operation_id,))
    arun(process(worker_settings,fake));s=view(c,g,s)
    assert fake.calls==0 and s['submissions'][0]['error_code']=='execution_outcome_unknown'
    assert submit(c,g,s).status_code==202


def test_lifetime_resource_quota_is_not_reset_by_new_exam(written,worker_settings,pg_settings):
    c,g=written;s=create(c,g).json()
    query(pg_settings[1],'UPDATE app.career_goals SET written_submissions=11 WHERE id=%s RETURNING id',(g['id'],))
    assert submit(c,g,s).status_code==202;arun(process(worker_settings,FakeExecutor()));s=view(c,g,s)
    assert submit(c,g,s).json()['code']=='written_execution_budget_exhausted'
    end(c,g,s);assert create(c,g).json()['code']=='written_execution_budget_exhausted'


def test_supported_source_bytes_also_fit_escaped_json_wire_body(written):
    c,g=written;s=create(c,g).json()
    source='x'+'\\'*8191
    assert len(source.encode())==8192
    r=submit(c,g,s,source)
    assert r.status_code==202,r.text
    s=view(c,g,s)
    assert s['submissions'][0]['source']==source
    too_large=c.post(base(g)+f"/written/{s['id']}/submissions",content='x'*65537,headers={'content-type':'application/json'})
    assert too_large.status_code==413
    old=c.post('/api/auth/sign-up/email',content='x'*16385,headers={'content-type':'application/json'})
    assert old.status_code==413


def test_confirmed_retest_hypothesis_selects_task_and_retraction_changes_next_selection(written,worker_settings):
    from test_interview_repository import create as start_interview,process as process_interview,view as interview_view,answer
    c,g=written
    session=start_interview(c,g,max_questions=4).json(); arun(process_interview(worker_settings)); session=interview_view(c,g,session)
    for text in ('需要原子更新。','每条语句取快照。','不知道，只用SETNX。','需要原子比较归属。'):
        assert answer(c,g,session,text).status_code==202; arun(process_interview(worker_settings));session=interview_view(c,g,session)
    assert session['status']=='COMPLETED'
    s=create(c,g).json(); assert s['task']['id']=='lock-owner-expiry'
    basis=s['selection_basis'];assert basis['origin']=='deterministic_retest_rule' and basis['observation_id']
    end(c,g,s)
    # F07Z-74: the interviewer can leave more than one needs_probe observation on the same topic,
    # so one retraction does not necessarily empty the hypothesis. Withdraw them one by one and
    # check both halves of the rule: while another unretracted observation remains the retest is
    # still selected, and once none remain the default task comes back.
    origins=[]
    for _ in range(4):
        assert post(c,base(g)+f"/interview-memory/{basis['observation_id']}/retract",
                    {'reason':'这条需要追问的观察撤回。'}).status_code==200
        nxt=create(c,g).json(); end(c,g,nxt)
        origins.append(nxt['selection_basis']['origin'])
        if nxt['task']['id']=='idempotency-record':
            assert nxt['selection_basis']['origin']=='default_backend_task'
            break
        assert nxt['task']['id']=='lock-owner-expiry'
        assert nxt['selection_basis']['origin']=='deterministic_retest_rule'
        basis=nxt['selection_basis']
    else:
        raise AssertionError(f'retraction never cleared the retest hypothesis: {origins}')
    assert origins.count('deterministic_retest_rule')>=1
