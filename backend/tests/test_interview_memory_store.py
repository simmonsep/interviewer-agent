"""S02: the Store projection writes only eligible evidence, is idempotent and fails open."""
from uuid import uuid4

import pytest

from interview_agent.interviews import memory_store
from interview_agent.interviews.memory_store import (
    ITEM_SCHEMA_VERSION, MemoryProjector, eligible, namespace, payload, plan)
from test_interview_repository import base, completed, context, post
from test_postgres import arun, pg_settings, query
from test_worker import worker_settings  # noqa: F401  (fixture)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture(scope='session')
def memory(pg_settings):
    owner = pg_settings[1]
    return arun(memory_store.initialize(owner, pg_settings[0].database.user))


def observation(**overrides):
    row = {'id': str(uuid4()), 'goal_id': 'g-1', 'answer_id': 'a-1', 'topic_id': 'java-concurrency',
           'label': 'needs_probe', 'assisted': False, 'origin': 'scripted_protocol',
           'pack_version': 'pack-1', 'correction': None, 'created_at': None}
    return row | overrides


def session(user_id='u-1'):
    return {'id': 's-1', 'goal_id': 'g-1', 'goal_version': 3, 'user_id': user_id, 'role': 'java-backend'}


# ---------------------------------------------------------------- offline contracts

@pytest.mark.parametrize('overrides,expected', [
    ({}, True),
    ({'correction': 'retracted'}, False),
    ({'label': 'inconclusive'}, False),
    ({'topic_id': None}, False),
    ({'answer_id': None}, False),
    ({'label': 'supported_in_this_answer'}, True),
])
def test_the_eligibility_gate(overrides, expected):
    assert eligible(observation(**overrides)) is expected


def test_the_projected_payload_never_carries_answer_text_or_a_summary():
    item = payload(observation(), session_id='s-1', goal_version=3, role='java-backend',
                   pack_version='pack-1', observed_at=None)
    assert item['schema_version'] == ITEM_SCHEMA_VERSION
    assert item['verification'] == 'training_observation_not_verified_ability'
    assert set(item) == {'schema_version', 'session_id', 'goal_id', 'goal_version', 'role', 'pack_version',
                         'answer_id', 'topic_id', 'label', 'assisted', 'origin', 'observed_at', 'verification'}
    assert not any('quote' in key or 'summary' in key or 'text' in key for key in item)


def test_namespace_is_scoped_to_account_goal_and_role():
    assert namespace('u-1', 'g-1', 'java-backend') == ('u-1', 'g-1', 'java-backend', 'interview-observations')


def test_plan_writes_eligible_rows_and_marks_revoked_keys_stale():
    kept, dropped = observation(), observation(correction='retracted')
    ns, items, stale = plan(session(), [kept, dropped])
    assert len(items) == 1 and stale == [(ns, dropped['id'])]
    (_, key, value), = items
    assert key == kept['id'] and ns[-1] == 'interview-observations'
    assert value['topic_id'] == 'java-concurrency' and value['session_id'] == 's-1'


# ---------------------------------------------------------------- real Store roundtrip

def test_roundtrip_topic_filter_and_idempotent_reprojection(pg_settings, memory):
    database = pg_settings[0].database
    # A fresh namespace per run: the Store persists, so a fixed one would accumulate rows.
    user_id, goal_id = str(uuid4()), str(uuid4())
    ns = namespace(user_id, goal_id, 'java-backend')
    _, items, _stale = plan({'id': 's-1', 'goal_id': goal_id, 'goal_version': 3, 'user_id': user_id,
                             'role': 'java-backend'},
                            [observation(goal_id=goal_id), observation(goal_id=goal_id, topic_id='sql-isolation')])

    async def scenario():
        async with memory_store.store(database) as value:
            await memory_store.project(value, items)
            first = await memory_store.search(value, ns, topics=['java-concurrency'])
            await memory_store.project(value, items)  # same keys again
            again = await memory_store.search(value, ns)
            return first, again

    first, again = arun(scenario())
    assert [item['topic_id'] for item in first] == ['java-concurrency']
    assert len(again) == 2  # idempotent: four writes, two keys


def test_a_finished_session_projects_and_a_retracted_observation_disappears(context, worker_settings, pg_settings):
    client, goal = context
    done = completed(client, goal, worker_settings)
    uid = query(pg_settings[1], 'SELECT user_id FROM app.career_goals WHERE id=%s', (goal['id'],))[0]['user_id']

    async def project_and_read(topics=None):
        from interview_agent.worker.queue import QueueTransport
        from test_worker import resources
        async with resources(worker_settings) as (pool_repo, _queue):
            projector = MemoryProjector(pg_settings[0].database)
            written = await projector.project_session(pool_repo.pool, done['id'])
            async with memory_store.store(pg_settings[0].database) as value:
                hits = await memory_store.search(value, namespace(uid, goal['id'], 'java-backend'), topics=topics)
            return written, hits

    written, hits = arun(project_and_read())
    assert written >= 1 and hits
    assert {item['session_id'] for item in hits} == {done['id']}
    assert all(item['verification'] == 'training_observation_not_verified_ability' for item in hits)

    observations = client.get(base(goal) + '/interview-memory').json()['items']
    target = observations[0]['id']
    post(client, base(goal) + f'/interview-memory/{target}/retract', {'reason': '这条观察需要更正。'})
    _, after = arun(project_and_read())
    assert target not in {item['key'] for item in after}
    assert after  # the other observation is still there


def test_gap_report_latest_summary_also_ignores_a_retracted_observation(context, worker_settings):
    client, goal = context
    completed(client, goal, worker_settings)
    observations = client.get(base(goal) + '/interview-memory').json()['items']
    target = observations[0]
    post(client, base(goal) + f"/interview-memory/{target['id']}/retract", {'reason': '最新一条需要更正。'})
    report = client.get(base(goal) + '/gap-report').json()
    wanted = target['payload']['topic_id']
    topic = next(item for item in report['topics'] if item['topic_id'] == wanted)
    assert topic['last_answer_quote'] != target['payload']['answer_quote']
    assert topic['attempts'] >= 1


def test_a_projection_failure_is_fail_open(pg_settings, worker_settings):
    broken = pg_settings[0].database.model_copy(update={'port': 1})
    projector = MemoryProjector(broken)

    async def scenario():
        from test_worker import resources
        async with resources(worker_settings) as (pool_repo, _queue):
            return await projector.project_session(pool_repo.pool, '00000000-0000-0000-0000-000000000000')

    assert arun(scenario()) == 0


# ---------------------------------------------------------------- S02b: read path and aggregation

class FakeProjector:
    """Stands in for the Store: returns fixed ids, or raises to exercise the SQL fallback."""

    def __init__(self, ids=None, error=None):
        self.ids, self.error, self.calls = ids or [], error, []

    async def candidates(self, user_id, goal_id, role, *, topics=None, limit=6):
        self.calls.append({'user_id': str(user_id), 'goal_id': str(goal_id), 'role': role})
        if self.error is not None:
            raise self.error
        return self.ids


def frozen_prior(client, goal, worker_settings, pg_settings, projector, mode='coaching'):
    """Create one more session with an injected projector and read its frozen payload."""
    from interview_agent.interviews.contracts import CreateInterview
    from interview_agent.interviews.repository import InterviewRepository
    from test_worker import resources

    user_id = query(pg_settings[1], 'SELECT user_id FROM app.career_goals WHERE id=%s', (goal['id'],))[0]['user_id']

    async def scenario():
        async with resources(worker_settings) as (pool_repo, _queue):
            repo = InterviewRepository(pool_repo.pool, projector=projector)
            value = CreateInterview(expected_version=1, role='java-backend', mode=mode, max_questions=2)
            session_id, _created = await repo.create_for_owner(user_id, goal['id'], uuid4().hex, value)
            async with pool_repo.pool.connection() as conn:
                row = await (await conn.execute('SELECT input_payload FROM app.career_interviews WHERE id=%s',
                                                (session_id,))).fetchone()
            return row['input_payload']['prior_observations']

    return arun(scenario())


def test_a_formal_interview_starts_without_cross_session_evidence(context, worker_settings, pg_settings):
    client, goal = context
    completed(client, goal, worker_settings)  # first finished session
    # One session is created here; the coaching side is covered by the fallback/revocation tests.
    assert frozen_prior(client, goal, worker_settings, pg_settings, None, mode='interview') == []


def test_a_stale_store_hit_cannot_resurrect_a_retracted_observation(context, worker_settings, pg_settings):
    client, goal = context
    completed(client, goal, worker_settings)
    observations = client.get(base(goal) + '/interview-memory').json()['items']
    target = observations[0]['id']
    post(client, base(goal) + f'/interview-memory/{target}/retract', {'reason': '这条需要更正。'})
    # The Store still answers with the revoked key; SQL re-validation must drop it.
    prior = frozen_prior(client, goal, worker_settings, pg_settings, FakeProjector([target]))
    assert target not in {item['id'] for item in prior}


def test_a_store_failure_falls_back_to_the_same_sql_query(context, worker_settings, pg_settings):
    client, goal = context
    completed(client, goal, worker_settings)
    projector = FakeProjector(error=RuntimeError('store_unavailable'))
    prior = frozen_prior(client, goal, worker_settings, pg_settings, projector)
    assert projector.calls and prior  # SQL answered, and the Store was actually attempted


def test_two_sessions_in_the_same_direction_are_a_repeated_observation(context, worker_settings):
    client, goal = context
    completed(client, goal, worker_settings)
    completed(client, goal, worker_settings)
    report = client.get(base(goal) + '/gap-report').json()
    topic = max(report['topics'], key=lambda item: item['attempts'])
    assert topic['sessions'] >= 2 and topic['repeated'] is True
    assert report['summary']['repeated_topics'] >= 1
    assert report['summary']['signal'] in ('repeated', 'conflicting')


def test_one_session_alone_is_never_a_repeated_observation(context, worker_settings):
    client, goal = context
    completed(client, goal, worker_settings)
    report = client.get(base(goal) + '/gap-report').json()
    assert all(item['repeated'] is False for item in report['topics'])
    assert report['summary']['repeated_topics'] == 0


def test_backfill_projects_finished_sessions_idempotently(context, worker_settings, pg_settings):
    client, goal = context
    done = completed(client, goal, worker_settings)
    database = pg_settings[0].database
    first = arun(memory_store._backfill(pg_settings[1], database, 5))
    second = arun(memory_store._backfill(pg_settings[1], database, 5))
    assert first['items'] == second['items'] >= 0
    uid = query(pg_settings[1], 'SELECT user_id FROM app.career_goals WHERE id=%s', (goal['id'],))[0]['user_id']
    hits = arun(_search(uid, goal['id'], database))
    if first['items']:
        assert all(hit['session_id'] for hit in hits)


async def _search(user_id, goal_id, database):
    async with memory_store.store(database) as value:
        return await memory_store.search(value, namespace(user_id, goal_id, 'java-backend'), limit=10)
