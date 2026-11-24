"""F07Z-166 D1: a same-context reply carries the selection into the new goal version."""
from uuid import uuid4

import pytest

from interview_agent.interview_prep import checkpointer
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for as career_worker
from interview_agent.interview_prep.worker import worker_for as prep_worker
from interview_agent.worker.queue import OutboxDispatcher
from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import resources, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

JD = '岗位职责：负责 Java 服务端开发。'


@pytest.fixture(scope='session')
def checkpoints(pg_settings):
    return arun(checkpointer.initialize(pg_settings[1], pg_settings[0].database.user))


@pytest.fixture
def goal(pg_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        uid = signup(client)['user']['id']
        value = post(client, '/api/career-goals/', {
            'message': '练一场 Java 后端面试', 'conditions': {'direction': 'Java后端'}, 'jd_text': JD}).json()
        yield client, value
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def post(client, path, body, key=None):
    return client.post(path, json=body, headers={'Idempotency-Key': key or uuid4().hex})


def base(goal):
    return f"/api/career-goals/{goal['id']}"


def selected(client, goal, acknowledge=True):
    job = post(client, base(goal) + '/jobs/manual', {
        'expected_version': 1, 'company': '合成公司', 'title': 'Java实习', 'cities': [],
        'jd_text': JD}).json()
    item = next(x for x in job['items'] if x['snapshot_id'] == job['snapshot_id'])
    response = post(client, base(goal) + '/selection', {
        'expected_version': 1, 'run_id': item['run_id'], 'snapshot_id': item['snapshot_id'],
        'acknowledge_unknown': acknowledge})
    assert response.status_code == 200, response.text
    return item


def reply(client, goal, **overrides):
    body = {'expected_version': 1, 'message': '开始训练', 'jd_text': JD,
            'conditions': {'direction': 'Java后端'}, 'intent': 'training', 'request': 'training'}
    body.update(overrides)
    response = post(client, base(goal) + '/messages', body)
    assert response.status_code == 202, response.text
    return response


def selections(owner, goal_id):
    return query(owner, '''SELECT goal_version,snapshot_id,carried_from_version
        FROM app.career_job_selections WHERE goal_id=%s ORDER BY goal_version''', (goal_id,))


def test_a_same_context_reply_materialises_the_selection_for_the_new_version(goal, pg_settings):
    client, value = goal
    item = selected(client, value)
    reply(client, value)
    rows = selections(pg_settings[1], value['id'])
    assert [(r['goal_version'], str(r['snapshot_id']), r['carried_from_version']) for r in rows] == [
        (1, item['snapshot_id'], None), (2, item['snapshot_id'], 1)]


def test_changing_the_jd_blocks_the_carry(goal, pg_settings):
    client, value = goal
    selected(client, value)
    reply(client, value, jd_text='岗位职责：负责 Go 服务端开发。')
    assert [r['goal_version'] for r in selections(pg_settings[1], value['id'])] == [1]


def test_changing_conditions_blocks_the_carry(goal, pg_settings):
    client, value = goal
    selected(client, value)
    reply(client, value, conditions={'direction': 'AI 应用'})
    assert [r['goal_version'] for r in selections(pg_settings[1], value['id'])] == [1]


def test_another_hard_unknown_blocks_the_carry(goal, pg_settings):
    """availability is exempt; a real condition-level unknown is not."""
    client, value = goal
    # Add a condition whose requirement is not stated in the JD, so checks() reports a hard
    # graduation unknown on top of availability.
    response = post(client, base(value) + '/messages', {
        'expected_version': 1, 'message': '补一个毕业年份', 'conditions': {'direction': 'Java后端', 'graduation_year': 2027}})
    assert response.status_code == 202, response.text
    value = client.get(base(value)).json()
    assert value['version'] == 2
    job = post(client, base(value) + '/jobs/manual', {
        'expected_version': 2, 'company': '合成公司', 'title': 'Java实习', 'cities': [], 'jd_text': JD}).json()
    item = next(x for x in job['items'] if x['snapshot_id'] == job['snapshot_id'])
    assert post(client, base(value) + '/selection', {
        'expected_version': 2, 'run_id': item['run_id'], 'snapshot_id': item['snapshot_id'],
        'acknowledge_unknown': True}).status_code == 200
    reply(client, value, expected_version=2,
          conditions={'direction': 'Java后端', 'graduation_year': 2027})
    assert [r['goal_version'] for r in selections(pg_settings[1], value['id'])] == [2]


def test_a_superseded_position_snapshot_blocks_the_carry(goal, pg_settings):
    client, value = goal
    item = selected(client, value)
    revision = post(client, base(value) + f"/jobs/{item['position_id']}/revisions", {
        'expected_version': 1, 'company': '合成公司', 'title': 'Java实习', 'cities': [],
        'jd_text': '岗位职责：负责 Java 服务端开发，新增一条要求。'})
    assert revision.status_code == 201, revision.text
    reply(client, value)
    assert [r['goal_version'] for r in selections(pg_settings[1], value['id'])] == [1]


def test_the_plan_says_where_the_selection_came_from(goal, pg_settings, worker_settings, checkpoints):
    client, value = goal
    item = selected(client, value)
    reply(client, value)

    async def process():
        async with resources(worker_settings) as (old, queue):
            repo = CareerRepository(old.pool)
            for _ in range(20):
                await OutboxDispatcher(repo, queue).dispatch()
                if await career_worker(repo, queue, mode='scripted').once():
                    break
            prep = PreparationRepository(old.pool)
            await OutboxDispatcher(prep, queue).dispatch()
            await prep_worker(prep, queue, pool=old.pool,
                              saver_factory=lambda: checkpointer.saver(worker_settings.database)).once()

    arun(process())
    current = client.get(base(value) + '/interview-preparations/current').json()
    assert current.get('status') == 'WAITING_FOR_USER', current
    assert current['plan']['selection'] == {'snapshot_id': item['snapshot_id'],
                                            'carried_from_version': 1}


def test_clearing_the_carried_selection_is_explicit_and_versioned(goal, pg_settings):
    client, value = goal
    selected(client, value)
    reply(client, value)
    assert client.delete(base(value) + '/selection?expected_version=1').status_code == 409
    cleared = client.delete(base(value) + '/selection?expected_version=2')
    assert cleared.status_code == 200, cleared.text
    assert [r['goal_version'] for r in selections(pg_settings[1], value['id'])] == [1]
