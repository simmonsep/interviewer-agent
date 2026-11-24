"""F07Z-104: the budget fuses keep one code but name the limit, and one long resume fits.

Arbitration Q3 required the diagnostic (`limit`) without renaming `model_budget_exhausted`;
Q4 required offline tests for the fit of a direct rewrite and for the separate gates.
"""
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import psycopg
import pytest

from interview_agent.billing.ledger import BillingUnavailable, authorize
from interview_agent.career.budget import NON_CODING_REQUEST_ALLOCATION
from interview_agent.materials.billing import MaterialLedger, MaterialPolicy
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.segments import (MAX_REQUESTS, narrative_batches,
                                                pre_heading_ids, request_ceiling,
                                                scope_segments)
from interview_agent.materials.worker import worker_for
from interview_agent.worker.queue import OutboxDispatcher
from test_billing import USAGE
from test_material_repository import (cleanup_auth, material_context, notification,
                                      source, start, view)
from test_postgres import arun, pg_settings, query
from test_worker import resources, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

# A 41-line resume: 6 header lines, 4 headings, 31 content lines (education 1, work 18,
# projects 6, skills 6) — the shape F07Z-101 rewrote live in nine requests.
LONG = '\n'.join([
    '张三 · 求职意向：Java 后端开发实习',
    '男 23 岁 · 广州 · 本科',
    '联系电话：134********',
    '邮箱：demo.user@example.com',
    '求职状态：应届生',
    '到岗时间：随时',
    '教育经历',
    '2014.09 - 2019.07 某大学 计算机科学与技术 本科',
    '工作经历',
    *[f'2018.10 - 2019.02 某科技公司 前端开发实习生（第 {n} 行）' for n in range(1, 19)],
    '项目经历',
    *[f'校园二手交易平台：负责订单服务与缓存（第 {n} 行）' for n in range(1, 7)],
    '技能',
    'Java / Spring Boot',
    'MySQL / Redis',
    'RabbitMQ / Docker',
    'Git / Linux',
    'JUnit / pytest',
    '英语 CET-6',
])


def _policy(project_root):
    return MaterialPolicy.load(project_root / 'configs/ja04-deepseek-flash.json')


def test_a_direct_rewrite_of_41_lines_fits_one_authorization(project_root):
    policy = _policy(project_root)
    scope = {'draft_style': 'rewrite', 'sources': [{'id': 's1', 'text': LONG}], 'facts': []}
    segments = scope_segments(scope)
    header = set(pre_heading_ids(segments))
    batches = narrative_batches([segment for segment in segments if segment['id'] not in header])
    assert len(batches) <= 6, f'{len(batches)} batches would need {request_ceiling(len(batches))}'
    cap = min(MAX_REQUESTS, policy.max_operation_attempts)
    assert request_ceiling(len(batches)) <= cap == 12
    assert request_ceiling(4) == 10
    # The rewrite needs no analysis, so the 12-request authorization is not split.
    assert policy.max_http_attempts == 12


@asynccontextmanager
async def _claimed_ledger(settings, policy, auth, note):
    async with resources(settings) as (resource_ctx, _queue):
        repo = MaterialRepository(resource_ctx.pool)
        claim = await repo.claim(note)
        scope = await repo.context(claim)
        yield claim, scope, MaterialLedger(resource_ctx.pool, policy, auth)

def _assert_limit(error, expected):
    assert error.value.code == 'model_budget_exhausted'
    assert error.value.limit == expected


def test_two_runs_at_the_same_cap_are_both_admitted(material_context, worker_settings, pg_settings,
                                                    project_root):
    """F07Z-120 (decision THIRD_OPTION): one authorization is an account, not one stage.

    The removed guard compared the authorization's whole call history with
    `max_http_attempts`, so the second nine-call rewrite at the same cap was refused as
    `authorization_http_limit`. The per-run fuse and the money cap are the only things that may
    stop a second run (9 + 9 = 18 calls on one authorization, which used to be impossible).
    """
    c, g = material_context
    source(c, g)
    policy = _policy(project_root)
    auth = 'test-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, auth))

    async def nine_calls(note):
        async with _claimed_ledger(worker_settings, policy, auth, note) as (claim, scope, ledger):
            for index in range(9):
                call = await ledger.reserve(claim, scope, f'{index:064d}', 100)
                await ledger.settle(call, usage=USAGE, http_status=200)

    def finish(run_id):
        # The goal allows one run in progress at a time; the ledger guards do not read the
        # status, so closing the run here is enough to start the second one. A non-RUNNING
        # status must also clear the lease (career_material_runs_check).
        query(pg_settings[1],
              "UPDATE app.career_material_runs SET status='SUCCEEDED',lease_until=NULL "
              'WHERE id=%s RETURNING id',
              (run_id,))

    try:
        for index in range(2):
            revision = view(c, g)['material_revision']
            response = start(c, g, revision, mode='draft', draft_style='rewrite')
            assert response.status_code == 202, (index, response.text)
            run_id = response.json()['run_id']
            arun(nine_calls(notification(pg_settings[1], run_id)))
            finish(run_id)
        total = query(pg_settings[1],
                      'SELECT count(*) AS n FROM app.model_http_calls WHERE authorization_id=%s',
                      (auth,))[0]['n']
        assert total == 18
    finally:
        cleanup_auth(pg_settings[1], auth)


def test_goal_material_check_allows_the_allocation_and_rejects_one_more(material_context,
                                                                        pg_settings):
    c, g = material_context
    query(pg_settings[1],
          'UPDATE app.career_goals SET material_model_requests=%s WHERE id=%s RETURNING id',
          (NON_CODING_REQUEST_ALLOCATION, g['id']))
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],
              'UPDATE app.career_goals SET material_model_requests=%s WHERE id=%s RETURNING id',
              (NON_CODING_REQUEST_ALLOCATION + 1, g['id']))


def test_run_request_check_still_rejects_seventeen(material_context, pg_settings):
    c, g = material_context
    source(c, g)
    rid = start(c, g, 1).json()['run_id']
    query(pg_settings[1],
          'UPDATE app.career_material_runs SET model_requests=%s WHERE id=%s RETURNING id',
          (MAX_REQUESTS, rid))
    with pytest.raises(psycopg.errors.CheckViolation):
        query(pg_settings[1],
              'UPDATE app.career_material_runs SET model_requests=%s WHERE id=%s RETURNING id',
              (MAX_REQUESTS + 1, rid))


def test_authorization_nano_limit_is_named(material_context, worker_settings, pg_settings,
                                           project_root):
    c, g = material_context
    source(c, g)
    rid = start(c, g, 1).json()['run_id']
    note = notification(pg_settings[1], rid)
    policy = _policy(project_root)
    auth = 'test-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, auth, 1))

    async def scenario():
        async with _claimed_ledger(worker_settings, policy, auth, note) as (claim, scope, ledger):
            with pytest.raises(BillingUnavailable) as error:
                await ledger.reserve(claim, scope, 'a' * 64, 100)
            _assert_limit(error, 'authorization_nano_limit')

    try:
        arun(scenario())
    finally:
        cleanup_auth(pg_settings[1], auth)


def test_goal_material_limit_is_named(material_context, worker_settings, pg_settings,
                                      project_root):
    c, g = material_context
    source(c, g)
    rid = start(c, g, 1).json()['run_id']
    note = notification(pg_settings[1], rid)
    policy = _policy(project_root)
    auth = 'test-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, auth))
    query(pg_settings[1], 'UPDATE app.career_goals SET material_model_requests=%s WHERE id=%s RETURNING id',
          (NON_CODING_REQUEST_ALLOCATION, g['id']))

    async def scenario():
        async with _claimed_ledger(worker_settings, policy, auth, note) as (claim, scope, ledger):
            with pytest.raises(BillingUnavailable) as error:
                await ledger.reserve(claim, scope, 'a' * 64, 100)
            _assert_limit(error, 'goal_material_request_limit')

    try:
        arun(scenario())
    finally:
        cleanup_auth(pg_settings[1], auth)


def test_run_attempt_limit_is_named(material_context, worker_settings, pg_settings,
                                    project_root):
    c, g = material_context
    source(c, g)
    rid = start(c, g, 1).json()['run_id']
    note = notification(pg_settings[1], rid)
    policy = _policy(project_root)
    auth = 'test-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, auth))
    query(pg_settings[1], 'UPDATE app.career_material_runs SET model_requests=%s WHERE id=%s RETURNING id',
          (min(MAX_REQUESTS, policy.max_operation_attempts), rid))

    async def scenario():
        async with _claimed_ledger(worker_settings, policy, auth, note) as (claim, scope, ledger):
            with pytest.raises(BillingUnavailable) as error:
                await ledger.reserve(claim, scope, 'a' * 64, 100)
            _assert_limit(error, 'run_operation_attempt_limit')

    try:
        arun(scenario())
    finally:
        cleanup_auth(pg_settings[1], auth)


def test_paused_run_records_the_limit_for_the_interface(material_context, worker_settings,
                                                       pg_settings, project_root, tmp_path):
    c, g = material_context
    source(c, g)
    start(c, g, 1)
    policy = _policy(project_root)
    auth = 'test-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, auth, 1))
    key = tmp_path / 'key.env'
    key.write_text('DEEPSEEK_API_KEY=offline-not-real\n')
    attempts = []

    def no_http(request):
        attempts.append(request)
        return httpx.Response(500)

    async def process():
        async with resources(worker_settings) as (old, queue):
            repo = MaterialRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            await worker_for(repo, queue, provider='live', policy=policy,
                             authorization=auth, credentials=key,
                             inner_factory=lambda: httpx.MockTransport(no_http)).once()

    try:
        arun(process())
        run = view(c, g)['runs'][0]
        assert run['status'] == 'WAITING_CREDENTIALS'
        assert run['error_code'] == 'model_budget_exhausted'
        assert run['result'] == {'limit': 'authorization_nano_limit'}
        assert attempts == []
        calls = query(pg_settings[1],
                      'SELECT count(*) AS n FROM app.model_http_calls WHERE authorization_id=%s', (auth,))
        assert calls[0]['n'] == 0
    finally:
        cleanup_auth(pg_settings[1], auth)
