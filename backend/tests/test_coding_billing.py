"""Real coding auth/PG/Redis/SDK with HTTPX MockTransport; no real Key, model or Docker."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from interview_agent.billing.ledger import authorize, BillingUnavailable
from interview_agent.career.repository import digest
from interview_agent.coding.agent import tools
from interview_agent.coding.billing import CodingPolicy, CodingLedger
from interview_agent.coding.repository import CodingRepository
from interview_agent.coding.worker import worker_for
from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
from interview_agent.runtime.types import AssistantResponse, ToolCall, Message
from interview_agent.worker.queue import QueueTransport, OutboxDispatcher
from test_billing import reply
from test_coding_repository import (IMAGE, FakeExecutor, solutions, coding,
                                    post, base, path, create, action, send, view, message)
from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import worker_settings, resources

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def live_coding(pg_settings, worker_settings):
    settings = pg_settings[0].model_copy(update={'coding_execution_image': IMAGE, 'coding_assistant_mode': 'live'})
    with client_for(settings) as c:
        c.headers['Origin'] = settings.origin
        uid = signup(c)['user']['id']
        g = post(c, '/api/career-goals/', {'message': '无简历，练习Java工程任务。', 'conditions': {'direction': 'Java后端'}}).json()
        yield c, g
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


@pytest.fixture
def live_auth(pg_settings, project_root, tmp_path):
    original = CodingPolicy.load(project_root / 'configs/ja07-deepseek-flash.json')
    key = tmp_path / 'offline-key.env'
    key.write_text('DEEPSEEK_API_KEY=offline-test-not-a-real-key\n', encoding='utf-8')
    ids = []

    def make(*, limit=2_000_000_000, overrides=None, registered=True):
        policy = CodingPolicy.model_validate({**original.model_dump(), **(overrides or {})})
        identity = 'coding-test-' + uuid4().hex
        if registered:
            arun(authorize(pg_settings[1], policy, identity, limit))
        ids.append(identity)
        return policy, identity, key

    yield make
    for identity in ids:
        query(pg_settings[1], 'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id', (identity,))
        query(pg_settings[1], 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id', (identity,))


async def process(settings, executor, *, repo_type=CodingRepository, **kwargs):
    async with resources(settings) as (old, base_queue):
        queue = QueueTransport(base_queue.redis, base_queue.stream + ':coding')
        try:
            repo = repo_type(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            assert await worker_for(repo, queue, executor, **kwargs).once()
        finally:
            await base_queue.redis.delete(queue.stream)


def sdk_steps(s, solution):
    """Known fixture sent through the real SDK; this double does not assess model ability."""
    selected = s['task']['editable'][0]
    state = {'n': 0, 'source_hash': None}

    def response(payload):
        state['n'] += 1
        n = state['n']
        last = json.loads(payload['messages'][-1]['content']) if payload['messages'][-1]['role'] == 'tool' else {}
        if n == 1:
            name, args = 'read_task', {}
        elif n == 2:
            name, args = 'list_files', {}
        elif n == 3:
            name, args = 'read_file', {'path': selected}
        elif n == 4:
            state['source_hash'] = last['sha256']
            name, args = 'run_public_tests', {}
        elif n == 5:
            assert last['result']['verdict'] == 'WRONG_ANSWER'
            name, args = 'replace_file', {'path': selected, 'expected_sha256': state['source_hash'], 'text': solution}
        elif n == 6:
            name, args = 'run_public_tests', {}
        else:
            assert n == 7 and last['result']['verdict'] == 'ACCEPTED'
            name, args = 'finish_coding', {'workspace_sha256': last['workspace_sha256'], 'status': 'verified',
                                          'explanation': '已按公开测试修复边界，并保留独立终评；这里只是SDK协议测试。'}
        return AssistantResponse(tool_calls=[ToolCall(id='sdk-coding-' + str(n), name=name, arguments=args)])

    return response


def start(c, g, **kwargs):
    s = create(c, g, **kwargs).json()
    assert s['policy']['assistant_provider'] == 'live'
    r = send(c, g, s, 'assist')
    assert r.status_code == 202, r.text
    return s


def test_long_legal_input_has_explicit_payload_failure_before_extra_http(live_coding, worker_settings, live_auth, solutions, pg_settings):
    from interview_agent.coding.workspace import file_hash
    c, g = live_coding; s = create(c, g).json(); filename = s['task']['editable'][0]
    text = '//' + '\\' * 7200 + '\n' + s['workspace']['files'][filename]
    assert len(text.encode()) <= 8192
    s = send(c, g, s, 'edits', path=filename, expected_sha256=file_hash(s['workspace']['files'][filename]),
             text=text, explanation='合法源码的请求体边界测试').json()
    assert send(c, g, s, 'assist').status_code == 202
    policy, identity, key = live_auth(); sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        name, args = ('read_task', {}) if len(sent) == 1 else ('read_file', {'path': filename})
        return httpx.Response(200, json=reply(AssistantResponse(tool_calls=[ToolCall(id='long-' + str(len(sent)), name=name, arguments=args)])))
    ex = FakeExecutor(solutions)
    arun(process(worker_settings, ex, provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(handler)))
    result = view(c, g, s); run = result['runs'][-1]
    assert run['error_code'] == 'model_payload_rejected' and len(sent) == 2 and not ex.calls
    assert query(pg_settings[1], 'SELECT count(*) AS n FROM app.model_http_calls WHERE authorization_id=%s', (identity,))[0]['n'] == 2


def test_sdk_reserves_before_send_settles_known_and_preserves_proposal_origin(live_coding, worker_settings, pg_settings, live_auth, solutions):
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth()
    choose = sdk_steps(s, solutions['ttl-boundary'])
    sent = []

    async def handler(request):
        async with resources(worker_settings) as (old, _):
            current = await CodingLedger(old.pool, policy, identity).summary()
            assert current['http_attempts'] == len(sent) + 1
            assert current['calls'][-1]['state'] == 'RESERVED'
        payload = json.loads(request.content)
        assert payload['model'] == 'deepseek-flash' and payload['thinking'] == {'type': 'disabled'}
        assert payload['stream'] is False and payload['max_tokens'] == 2048
        assert {x['function']['name'] for x in payload['tools']} == {x.name for x in tools()}
        sent.append(payload)
        return httpx.Response(200, json=reply(choose(payload)))

    ex = FakeExecutor(solutions)
    arun(process(worker_settings, ex, provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(handler)))
    result = view(c, g, s)
    run = result['runs'][-1]
    assert run['status'] == 'SUCCEEDED', run
    assert run['origin'] == 'real_model' and len(sent) == 7 and len(ex.calls) == 2
    assert result['workspace'] == s['workspace'] and len(result['versions']) == 1
    rows = query(pg_settings[1], 'SELECT state,purpose,accounted_nano FROM app.model_http_calls WHERE authorization_id=%s', (identity,))
    assert len(rows) == 7 and all(r['state'] == 'KNOWN' and r['purpose'] == 'career_coding' and r['accounted_nano'] > 0 for r in rows)
    goal = query(pg_settings[1], 'SELECT model_requests,coding_model_requests FROM app.career_goals WHERE id=%s', (g['id'],))[0]
    assert goal == {'model_requests': 7, 'coding_model_requests': 7}
    counts = query(pg_settings[1], 'SELECT model_requests,assistant_responses FROM app.career_coding_runs WHERE id=%s', (run['id'],))[0]
    assert counts == {'model_requests': 7, 'assistant_responses': 7}


@pytest.mark.parametrize('scope', ['goal', 'coding_goal', 'session', 'run', 'money'])
def test_cumulative_limits_reject_before_http(live_coding, worker_settings, pg_settings, live_auth, solutions, scope):
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth(limit=1 if scope == 'money' else 2_000_000_000)
    if scope == 'goal':
        query(pg_settings[1], 'UPDATE app.career_goals SET model_requests=120 WHERE id=%s RETURNING id', (g['id'],))
    elif scope == 'coding_goal':
        query(pg_settings[1], 'UPDATE app.career_goals SET coding_model_requests=48 WHERE id=%s RETURNING id', (g['id'],))
    elif scope == 'session':
        query(pg_settings[1], 'UPDATE app.career_coding_sessions SET model_requests=24 WHERE id=%s RETURNING id', (s['id'],))
    elif scope == 'run':
        query(pg_settings[1], 'UPDATE app.career_coding_runs SET model_requests=12 WHERE session_id=%s RETURNING id', (s['id'],))

    def forbidden(request): pytest.fail('billing admission must reject before outbound HTTP')

    arun(process(worker_settings, FakeExecutor(solutions), provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(forbidden)))
    result = view(c, g, s)
    assert result['runs'][-1]['status'] == 'FAILED'
    assert result['runs'][-1]['error_code'] == 'model_budget_exhausted'
    assert not query(pg_settings[1], 'SELECT id FROM app.model_http_calls WHERE authorization_id=%s', (identity,))


def test_missing_authorization_never_sends_http(live_coding, worker_settings, live_auth, solutions):
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth(registered=False)

    def forbidden(request): pytest.fail('unregistered authorization must not send HTTP')

    arun(process(worker_settings, FakeExecutor(solutions), provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(forbidden)))
    assert view(c, g, s)['runs'][-1]['error_code'] == 'model_authorization_unavailable'


def test_operation_attempt_budget_counts_actual_http(live_coding, worker_settings, live_auth, solutions, pg_settings):
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth(overrides={'max_operation_attempts': 1})
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=reply(AssistantResponse(tool_calls=[ToolCall(id='first-read', name='read_task', arguments={})])))

    arun(process(worker_settings, FakeExecutor(solutions), provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(handler)))
    assert len(sent) == 1 and view(c, g, s)['runs'][-1]['error_code'] == 'model_budget_exhausted'
    rows = query(pg_settings[1], 'SELECT state FROM app.model_http_calls WHERE authorization_id=%s', (identity,))
    assert rows == [{'state': 'KNOWN'}]


def test_unknown_usage_pauses_authorization_and_next_run_does_not_send(live_coding, worker_settings, live_auth, solutions, pg_settings):
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth()
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=reply(AssistantResponse(tool_calls=[ToolCall(id='unknown-read', name='read_task', arguments={})]), usage=None))

    kwargs = {'provider': 'live', 'policy': policy, 'authorization': identity, 'credentials': key,
              'inner_factory': lambda: httpx.MockTransport(handler)}
    arun(process(worker_settings, FakeExecutor(solutions), **kwargs))
    s = view(c, g, s)
    assert s['runs'][-1]['status'] == 'FAILED' and s['runs'][-1]['error_code'] == 'model_usage_unknown'
    assert send(c, g, s, 'assist').status_code == 202
    arun(process(worker_settings, FakeExecutor(solutions), **kwargs))
    assert len(sent) == 1 and view(c, g, s)['runs'][-1]['error_code'] == 'model_usage_unknown'
    rows = query(pg_settings[1], 'SELECT state,reserved_nano,accounted_nano FROM app.model_http_calls WHERE authorization_id=%s', (identity,))
    assert len(rows) == 1 and rows[0]['state'] == 'UNKNOWN' and rows[0]['reserved_nano'] == policy.reservation_nano and rows[0]['accounted_nano'] is None
    assert query(pg_settings[1], 'SELECT state FROM app.model_authorizations WHERE id=%s', (identity,)) == [{'state': 'PAUSED'}]


def test_hidden_answer_canary_never_enters_sdk_payload(live_coding, worker_settings, live_auth, solutions, monkeypatch):
    from interview_agent.coding.catalog import task as original_task
    canary = 'PRIVATE-HIDDEN-ANSWER-' + uuid4().hex

    def authored(identity):
        value = original_task(identity)
        value['cases'][1]['output'] = canary
        value['sha256'] = digest({k: v for k, v in value.items() if k != 'sha256'})
        return value

    monkeypatch.setattr('interview_agent.coding.repository.task', authored)
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth()
    choose = sdk_steps(s, solutions['ttl-boundary'])
    sent = []

    def handler(request):
        payload = json.loads(request.content)
        assert canary not in request.content.decode('utf-8')
        assert 'zero-ttl' not in request.content.decode('utf-8')
        sent.append(payload)
        return httpx.Response(200, json=reply(choose(payload)))

    arun(process(worker_settings, FakeExecutor(solutions), provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(handler)))
    assert view(c, g, s)['runs'][-1]['status'] == 'SUCCEEDED' and len(sent) == 7


def test_cancelled_claim_cannot_reserve_or_reach_sdk_http(live_coding, worker_settings, live_auth, solutions, pg_settings):
    c, g = live_coding
    s = start(c, g)
    note = message(pg_settings[1], s['id'])
    policy, identity, key = live_auth()

    async def run():
        async with resources(worker_settings) as (old, _):
            repo = CodingRepository(old.pool)
            claim = await repo.claim(note)
            assert await repo.begin(claim, 'real_model')
            await repo.admit(claim, 'response')
            scope = (await repo.context(claim))['input_payload']
            current = view(c, g, s)
            assert post(c, path(g, s) + '/cancel', {'expected_version': current['version']}).status_code == 200

            def forbidden(request): pytest.fail('cancelled execution right must prevent actual HTTP')

            provider = DeepSeekProvider('offline-test-not-a-real-key', BudgetedTransport(httpx.MockTransport(forbidden), CodingLedger(old.pool, policy, identity), claim, scope))
            try:
                with pytest.raises(BillingUnavailable, match='model_execution_expired'):
                    await provider.complete([Message(role='user', text='仅验证取消准入')], tools())
            finally:
                await provider.aclose()
    arun(run())
    assert not query(pg_settings[1], 'SELECT id FROM app.model_http_calls WHERE authorization_id=%s', (identity,))


def test_old_attempt_and_changed_scope_cannot_reserve(live_coding, worker_settings, live_auth, pg_settings):
    c, g = live_coding
    s = start(c, g)
    note = message(pg_settings[1], s['id'])
    policy, identity, _ = live_auth()

    async def run():
        async with resources(worker_settings) as (old, _):
            repo = CodingRepository(old.pool); first = await repo.claim(note)
            original = (await repo.context(first))['input_payload']
            async with old.pool.connection() as conn:
                await conn.execute("UPDATE app.career_coding_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (note.operation_id,))
            second = await repo.claim(note); assert second.attempt == 2
            assert await repo.begin(second, 'real_model')
            ledger = CodingLedger(old.pool, policy, identity)
            with pytest.raises(BillingUnavailable, match='model_execution_expired'):
                await ledger.reserve(first, original, 'a' * 64, 100)
            wrong = deepcopy(original); wrong['files']['ExpiringCache.java'] += '\n// different scope\n'
            with pytest.raises(BillingUnavailable, match='model_context_changed'):
                await ledger.reserve(second, wrong, 'b' * 64, 100)
            assert (await ledger.summary())['http_attempts'] == 0
    arun(run())


def test_cached_live_candidate_reclaim_uses_zero_additional_model_or_execution(live_coding, worker_settings, live_auth, solutions, pg_settings):
    c, g = live_coding
    s = start(c, g)
    policy, identity, key = live_auth()
    choose = sdk_steps(s, solutions['ttl-boundary']); sent = []

    def handler(request):
        payload = json.loads(request.content); sent.append(payload)
        return httpx.Response(200, json=reply(choose(payload)))

    class CacheOnly(CodingRepository):
        async def complete(self, claim): return False

    ex = FakeExecutor(solutions)
    arun(process(worker_settings, ex, repo_type=CacheOnly, provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(handler)))
    row = query(pg_settings[1], 'SELECT id,status,candidate FROM app.career_coding_runs WHERE session_id=%s', (s['id'],))[0]
    assert row['status'] == 'RUNNING' and row['candidate'] is not None and len(sent) == 7
    query(pg_settings[1], "UPDATE app.career_coding_runs SET lease_until=clock_timestamp()-interval '1 second',dispatched_at=NULL WHERE id=%s RETURNING id", (row['id'],))
    arun(process(worker_settings, None, provider='disabled'))
    result = view(c, g, s)['runs'][-1]
    assert result['status'] == 'SUCCEEDED' and result['attempt'] == 2 and result['origin'] == 'real_model'
    assert len(sent) == 7 and len(ex.calls) == 2
    assert len(query(pg_settings[1], 'SELECT id FROM app.model_http_calls WHERE authorization_id=%s', (identity,))) == 7


def test_worker_cannot_relabel_scripted_session_as_live(coding, worker_settings, live_auth, solutions, pg_settings):
    c, g = coding
    s = create(c, g).json(); assert s['policy']['assistant_provider'] == 'scripted'
    assert send(c, g, s, 'assist').status_code == 202
    policy, identity, key = live_auth()

    def forbidden(request): pytest.fail('frozen provider policy must precede HTTP')

    arun(process(worker_settings, FakeExecutor(solutions), provider='live', policy=policy, authorization=identity, credentials=key,
                 inner_factory=lambda: httpx.MockTransport(forbidden)))
    r = view(c, g, s)['runs'][-1]
    assert r['status'] == 'FAILED' and r['error_code'] == 'coding_provider_policy_mismatch'
    assert not query(pg_settings[1], 'SELECT id FROM app.model_http_calls WHERE authorization_id=%s', (identity,))
