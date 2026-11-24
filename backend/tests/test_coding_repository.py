"""Actual authentication/API/PG/Redis; injected executor is labelled, not a Docker result."""
import asyncio
from dataclasses import replace
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from interview_agent.career.repository import digest
from interview_agent.coding.repository import CodingRepository
from interview_agent.coding.worker import worker_for
from interview_agent.coding.workspace import file_hash
from interview_agent.contracts.operations import QueueMessage
from interview_agent.worker.queue import OutboxDispatcher, QueueTransport
from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import worker_settings, resources

pytestmark = [pytest.mark.postgres, pytest.mark.redis]
IMAGE = 'eclipse-temurin@sha256:' + 'a' * 64


@pytest.fixture
def solutions(project_root):
    return json.loads((project_root / 'fixtures/ja07/scripted-solutions.json').read_text(encoding='utf-8'))


@pytest.fixture
def coding(pg_settings, worker_settings):
    settings = pg_settings[0].model_copy(update={'coding_execution_image': IMAGE})
    with client_for(settings) as c:
        c.headers['Origin'] = settings.origin; user = signup(c)['user']['id']
        g = post(c, '/api/career-goals/', {'message': '无简历，Java后端AI Coding训练', 'conditions': {'direction': 'Java后端'}}).json()
        yield c, g
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))


def post(c, path, body, key=None):
    return c.post(path, json=body, headers={'Idempotency-Key': key or uuid4().hex})


def base(g): return f"/api/career-goals/{g['id']}"
def path(g, s): return base(g) + '/coding/' + s['id']
def create(c, g, **kw): return post(c, base(g) + '/coding', {'expected_version': 1, **kw})
def action(s): return {'expected_version': s['version'], 'expected_workspace_sha256': s['workspace']['sha256']}
def view(c, g, s): return c.get(path(g, s)).json()
def send(c, g, s, name, **kw): return post(c, path(g, s) + '/' + name, {**action(s), **kw})


class FakeExecutor:
    profile = SimpleNamespace(image=IMAGE)
    def __init__(self, solutions): self.solutions, self.calls = solutions, []
    async def grade(self, files, cases, lost=None):
        self.calls.append({'files': dict(files), 'cases': cases})
        passed = any(solution in files.values() for solution in self.solutions.values())
        verdict = 'ACCEPTED' if passed else 'WRONG_ANSWER'
        return {'verdict': verdict, 'cases': [{'id': c['id'], 'visibility': c['visibility'], 'verdict': verdict} for c in cases],
                'evidence': {'workspace_sha256': digest(files), 'image': IMAGE, 'policy': 'ja06-docker-java-v1', 'workspace_policy': 'ja07-flat-java-v1'}}


async def process(settings, solutions, executor=None, provider='scripted'):
    async with resources(settings) as (old, q):
        queue = QueueTransport(q.redis, q.stream + ':coding')
        try:
            repo = CodingRepository(old.pool); await OutboxDispatcher(repo, queue).dispatch()
            assert await worker_for(repo, queue, executor, provider=provider, solutions=solutions).once()
        finally: await q.redis.delete(queue.stream)


def message(owner, sid):
    r = query(owner, 'SELECT id,job_id,goal_version FROM app.career_coding_runs WHERE session_id=%s ORDER BY created_at DESC LIMIT 1', (sid,))[0]
    return QueueMessage(operation_id=str(r['id']), job_id=str(r['job_id']), resource_version=r['goal_version'])


@pytest.mark.parametrize('identity', ['ttl-boundary', 'order-cancel'])
def test_assistant_proposes_user_accepts_frozen_final_and_refresh(coding, worker_settings, solutions, identity):
    c, g = coding; s = create(c, g, task_id=identity).json(); initial = s['workspace']
    assert s['status'] == 'ACTIVE' and len(s['versions']) == 1 and not s['ability_certified']
    assert 'zero-ttl' not in str(s) and 'cancel-repeat' not in str(s)
    r = send(c, g, s, 'assist'); assert r.status_code == 202, r.text
    ex = FakeExecutor(solutions); arun(process(worker_settings, solutions, ex))
    s = view(c, g, s); run = s['runs'][-1]
    assert run['status'] == 'SUCCEEDED', run
    assert s['workspace'] == initial and len(ex.calls) == 2 and all(len(call['cases']) == 1 for call in ex.calls)
    assert run['assistant_responses'] == 7 and run['origin'] == 'scripted_protocol'
    assert run['result']['proposal']['tests'][0]['result']['verdict'] == 'WRONG_ANSWER'
    s = send(c, g, s, 'decisions', run_id=run['id'], decision='accept', explanation='我已检查diff，接受这个补丁。').json()
    assert s['workspace']['sha256'] != initial['sha256'] and s['versions'][-1]['origin'] == 'assistant'
    final = send(c, g, s, 'submit', explanation='我解释边界条件，并提交当前版本。')
    assert final.status_code == 202 and final.json()['session']['status'] == 'SUBMITTED'
    assert send(c, g, final.json()['session'], 'assist').status_code == 409
    arun(process(worker_settings, solutions, ex)); s = view(c, g, s)
    assert s['status'] == 'COMPLETED' and s['runs'][-1]['result']['grade']['verdict'] == 'ACCEPTED'
    assert len(ex.calls[-1]['cases']) == 4 and s['runs'][-1]['user_explanation'].startswith('我解释')
    assert view(c, g, s)['versions'] == s['versions'] and s['goal_budget']['coding_executions'] == 3


def test_reject_keeps_user_code_and_records_decision(coding, worker_settings, solutions):
    c, g = coding; s = create(c, g).json(); initial = s['workspace']
    send(c, g, s, 'assist'); arun(process(worker_settings, solutions, FakeExecutor(solutions)))
    s = view(c, g, s); rid = s['runs'][0]['id']
    s = send(c, g, s, 'decisions', run_id=rid, decision='reject', explanation='我想自行实现。').json()
    assert s['workspace'] == initial and s['commands'][-1]['kind'] == 'reject'
    assert send(c, g, s, 'decisions', run_id=rid, decision='accept', explanation='再次接受').status_code == 409


def test_user_edit_is_append_only_and_idempotent(coding, solutions, pg_settings):
    c, g = coding; s = create(c, g).json(); p = 'ExpiringCache.java'; key = uuid4().hex
    body = {**action(s), 'path': p, 'expected_sha256': file_hash(s['workspace']['files'][p]), 'text': solutions['ttl-boundary'], 'explanation': 'now等于截止时刻也失效。'}
    a = post(c, path(g, s) + '/edits', body, key); b = post(c, path(g, s) + '/edits', body, key)
    assert a.status_code == b.status_code == 200 and len(b.json()['versions']) == 2
    assert b.json()['versions'][0]['files'][p] == s['workspace']['files'][p] and b.json()['versions'][1]['origin'] == 'user'
    assert post(c, path(g, s) + '/edits', {**body, 'explanation': 'changed'}, key).status_code == 409
    with pytest.raises(Exception, match='coding_append_only'):
        query(pg_settings[1], "UPDATE app.career_coding_versions SET files='{}' WHERE session_id=%s RETURNING id", (s['id'],))


def test_readonly_path_extra_policy_and_source_limits_rejected(coding):
    c, g = coding; s = create(c, g).json()
    assert create(c, g, command='curl').status_code == 400
    for p in ('Main.java', '../ExpiringCache.java', 'C:\\secret'):
        r = send(c, g, s, 'edits', path=p, expected_sha256=file_hash('x'), text='x', explanation='x')
        assert r.status_code == 400
    for text in ('中' * 3000, '\x00', ' '):
        assert send(c, g, s, 'edits', path='ExpiringCache.java', expected_sha256=file_hash('x'), text=text, explanation='x').status_code == 400


def test_escaped_8k_source_fits_only_the_new_edit_wire_boundary(coding):
    c, g = coding; s = create(c, g).json(); p = 'ExpiringCache.java'; code = '\\' * 8192
    r = send(c, g, s, 'edits', path=p, expected_sha256=file_hash(s['workspace']['files'][p]), text=code, explanation='合成字节边界')
    assert r.status_code == 200 and r.json()['workspace']['files'][p] == code
    assert send(c, g, r.json(), 'edits', path=p, expected_sha256=file_hash(code), text=code + 'x', explanation='超限').status_code == 400


def test_single_active_session_and_pending_run_prevents_edits(coding):
    c, g = coding; s = create(c, g).json()
    assert create(c, g).status_code == 409
    a = send(c, g, s, 'public-tests'); assert a.status_code == 202
    s = a.json()['session']
    assert send(c, g, s, 'assist').status_code == 409
    assert send(c, g, s, 'edits', path='ExpiringCache.java', expected_sha256=file_hash('x'), text='x', explanation='x').status_code == 409


def test_same_key_run_replay_and_different_kind_conflict(coding):
    c, g = coding; s = create(c, g).json(); key = uuid4().hex; body = action(s)
    a = post(c, path(g, s) + '/public-tests', body, key); b = post(c, path(g, s) + '/public-tests', body, key)
    assert a.status_code == 202 and b.status_code == 200 and a.json()['run_id'] == b.json()['run_id']
    assert post(c, path(g, s) + '/assist', body, key).status_code == 409


def test_cross_account_does_not_read_code_or_operate(coding, pg_settings):
    c, g = coding; s = create(c, g).json()
    with client_for(pg_settings[0].model_copy(update={'coding_execution_image': IMAGE})) as other:
        other.headers['Origin'] = pg_settings[0].origin; uid = signup(other)['user']['id']
        assert other.get(path(g, s)).status_code == 404
        assert send(other, g, s, 'assist').status_code == 404
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def test_disabled_api_and_worker_do_not_use_host_or_claim_success(coding, pg_settings, worker_settings, solutions):
    c, g = coding; s = create(c, g).json()
    send(c, g, s, 'public-tests'); arun(process(worker_settings, solutions, None))
    s = view(c, g, s); assert s['runs'][-1]['status'] == 'FAILED' and s['runs'][-1]['error_code'] == 'coding_executor_disabled'
    assert s['budget']['executions'] == 0
    # A separately signed in account checks default capability, without sharing cookies.
    with client_for(pg_settings[0]) as other:
        other.headers['Origin'] = pg_settings[0].origin; uid = signup(other)['user']['id']
        og = post(other, '/api/career-goals/', {'message': '合成默认关闭'}).json()
        assert create(other, og).status_code == 503
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def test_proposal_stale_after_user_edit(coding, worker_settings, solutions):
    c, g = coding; s = create(c, g).json(); send(c, g, s, 'assist')
    arun(process(worker_settings, solutions, FakeExecutor(solutions))); s = view(c, g, s); rid = s['runs'][-1]['id']; p = 'ExpiringCache.java'
    s = send(c, g, s, 'edits', path=p, expected_sha256=file_hash(s['workspace']['files'][p]), text=s['workspace']['files'][p] + '\n// user note\n', explanation='保留我的修改').json()
    assert send(c, g, s, 'decisions', run_id=rid, decision='accept', explanation='旧补丁').status_code == 409


def test_old_proposal_cannot_apply_even_if_user_restores_identical_bytes(coding, worker_settings, solutions):
    c, g = coding; s = create(c, g).json(); p = 'ExpiringCache.java'; original = s['workspace']['files'][p]
    send(c, g, s, 'assist'); arun(process(worker_settings, solutions, FakeExecutor(solutions)))
    s = view(c, g, s); rid = s['runs'][-1]['id']
    for code in (original + '\n// temporary user edit\n', original):
        s = send(c, g, s, 'edits', path=p, expected_sha256=file_hash(s['workspace']['files'][p]), text=code, explanation='用户变更版本').json()
    assert s['workspace']['sha256'] == digest(s['versions'][0]['files'])
    assert send(c, g, s, 'decisions', run_id=rid, decision='accept', explanation='旧版本提议').status_code == 409


def test_budget_exhaustion_stops_before_second_actual_execution(coding, worker_settings, solutions, pg_settings):
    c, g = coding; s = create(c, g).json(); send(c, g, s, 'assist')
    query(pg_settings[1], 'UPDATE app.career_goals SET coding_executions=35 WHERE id=%s RETURNING id', (g['id'],))
    ex = FakeExecutor(solutions); arun(process(worker_settings, solutions, ex)); s = view(c, g, s)
    assert len(ex.calls) == 1 and s['goal_budget']['coding_executions'] == 36
    assert s['runs'][-1]['status'] == 'FAILED' and s['runs'][-1]['error_code'] == 'coding_execution_budget_exhausted'


def test_mock_assistance_quota_and_goal_counts_are_not_reset_by_new_session(coding, worker_settings, solutions):
    c, g = coding; s = create(c, g, mode='mock').json(); send(c, g, s, 'assist')
    arun(process(worker_settings, solutions, FakeExecutor(solutions))); s = view(c, g, s)
    assert send(c, g, s, 'assist').status_code == 409
    old = s['goal_budget']; post(c, path(g, s) + '/cancel', {'expected_version': s['version']})
    second = create(c, g).json(); assert second['goal_budget'] == old and second['budget']['assistant_responses'] == 0


def test_run_and_scope_are_immutable(coding, pg_settings):
    c, g = coding; s = create(c, g).json(); r = send(c, g, s, 'public-tests').json()
    with pytest.raises(Exception, match='coding_input_immutable'):
        query(pg_settings[1], "UPDATE app.career_coding_runs SET input_payload='{}' WHERE id=%s RETURNING id", (r['run_id'],))
    with pytest.raises(Exception, match='coding_input_immutable'):
        query(pg_settings[1], "UPDATE app.career_coding_sessions SET input_payload='{}' WHERE id=%s RETURNING id", (s['id'],))


def test_started_without_candidate_fails_without_reexecution(coding, worker_settings, solutions, pg_settings):
    c, g = coding; s = create(c, g).json(); send(c, g, s, 'public-tests'); msg = message(pg_settings[1], s['id'])
    async def run():
        async with resources(worker_settings) as (old, _):
            repo = CodingRepository(old.pool); claim = await repo.claim(msg); assert await repo.begin(claim)
            async with repo.pool.connection() as conn:
                await conn.execute("UPDATE app.career_coding_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (msg.operation_id,))
            assert await repo.claim(msg) is None
    arun(run()); s = view(c, g, s)
    assert s['runs'][-1]['status'] == 'FAILED' and s['runs'][-1]['error_code'] == 'coding_outcome_unknown'


def test_cached_candidate_reclaims_without_execution(coding, worker_settings, solutions, pg_settings):
    c, g = coding; s = create(c, g).json(); send(c, g, s, 'public-tests'); msg = message(pg_settings[1], s['id'])
    async def run():
        async with resources(worker_settings) as (old, _):
            repo = CodingRepository(old.pool); claim = await repo.claim(msg); assert await repo.begin(claim)
            await repo.admit(claim, 'execution'); r = await repo.context(claim); p = r['input_payload']
            files = {k: v for k, v in p['files'].items() if k.endswith('.java')}; cases = [x for x in p['task']['cases'] if x['visibility'] == 'public']
            result = {'grade': await FakeExecutor(solutions).grade(files, cases), 'workspace_sha256': p['files_sha256']}
            assert await repo.save(claim, result)
            async with repo.pool.connection() as conn:
                await conn.execute("UPDATE app.career_coding_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (msg.operation_id,))
            newer = await repo.claim(msg); assert newer and newer.attempt == 2
            assert not await repo.save(claim, result)
            assert await repo.complete(newer)
    arun(run()); s = view(c, g, s)
    assert s['runs'][-1]['status'] == 'SUCCEEDED' and s['budget']['executions'] == 1


@pytest.mark.parametrize('change', ['goal', 'material', 'deadline', 'cancel'])
def test_context_change_deadline_or_cancel_fences_late_results(coding, worker_settings, pg_settings, change):
    c, g = coding; s = create(c, g).json(); send(c, g, s, 'public-tests'); s = view(c, g, s); msg = message(pg_settings[1], s['id'])
    async def claim_it():
        async with resources(worker_settings) as (old, _): return await CodingRepository(old.pool).claim(msg)
    claim = arun(claim_it()); assert claim
    if change == 'goal': query(pg_settings[1], 'UPDATE app.career_goals SET version=version+1 WHERE id=%s RETURNING id', (g['id'],))
    elif change == 'material': query(pg_settings[1], 'UPDATE app.career_material_state SET revision=revision+1 WHERE goal_id=%s RETURNING goal_id', (g['id'],))
    elif change == 'deadline': query(pg_settings[1], "UPDATE app.career_coding_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id", (s['id'],))
    else: assert post(c, path(g, s) + '/cancel', {'expected_version': s['version']}).status_code == 200
    s = view(c, g, s); assert s['status'] in ('STALE', 'EXPIRED', 'CANCELLED')
    async def late():
        async with resources(worker_settings) as (old, _):
            repo = CodingRepository(old.pool); assert not await repo.heartbeat(claim); assert not await repo.save(claim, {})
    arun(late()); assert s['runs'][-1]['status'] == 'CANCELLED'


def test_on_time_submission_runs_after_deadline_and_bad_code_is_not_process_failure(coding, worker_settings, solutions, pg_settings):
    c, g = coding; s = create(c, g).json(); r = send(c, g, s, 'submit', explanation='提交原始代码以验证错误反馈')
    assert r.status_code == 202
    query(pg_settings[1], "UPDATE app.career_coding_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id", (s['id'],))
    arun(process(worker_settings, solutions, FakeExecutor(solutions))); s = view(c, g, s)
    assert s['status'] == 'COMPLETED' and s['runs'][-1]['status'] == 'SUCCEEDED'
    assert s['runs'][-1]['result']['grade']['verdict'] == 'WRONG_ANSWER'


def test_cached_proposal_requires_current_source_and_frozen_execution_policy(coding, worker_settings, solutions, pg_settings):
    c, g = coding; s = create(c, g).json(); send(c, g, s, 'assist')
    arun(process(worker_settings, solutions, FakeExecutor(solutions))); s = view(c, g, s)
    row = query(pg_settings[1], 'SELECT * FROM app.career_coding_runs WHERE id=%s', (s['runs'][-1]['id'],))[0]
    repo = CodingRepository(None)
    for field, value in [('workspace_sha256', 'bad'), ('policy', 'different_policy'), ('workspace_policy', 'different_workspace_policy')]:
        result = deepcopy(row['result']); result['proposal']['tests'][-1]['result']['evidence'][field] = value
        with pytest.raises(ValueError): repo.validate(row, result)
