"""F07Z-166: one explicit training turn creates the preparation task; nothing else does."""
from uuid import uuid4

import pytest

from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for
from interview_agent.interview_prep.contracts import CreatePreparation
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.storage.errors import ApiError
from interview_agent.worker.queue import OutboxDispatcher
from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import resources, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def career(pg_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = signup(client)['user']['id']
        yield client, user
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))


def post(client, path, body):
    return client.post(path, json=body, headers={'Idempotency-Key': uuid4().hex})


async def process(settings, goal_id):
    """Run the scoped coordinator until *this* goal is no longer PENDING.

    The outbox is database-wide, so a shared test database may hold older pending steps from
    other goals. They are legitimate work, not this test's; keep draining until ours ran.
    """
    async with resources(settings) as (old, queue):
        repo = CareerRepository(old.pool)
        idle = 0
        for _ in range(200):
            dispatched = await OutboxDispatcher(repo, queue).dispatch()
            worked = await worker_for(repo, queue, mode='scripted').once()
            async with old.pool.connection() as conn:
                row = await (await conn.execute(
                    'SELECT status FROM app.career_goals WHERE id=%s', (goal_id,))).fetchone()
            if row['status'] != 'PENDING':
                return row['status']
            idle = 0 if (dispatched or worked) else idle + 1
            if idle >= 3:
                break
        raise AssertionError('goal_still_pending')


def preps(owner, goal_id):
    return query(owner, '''SELECT id,status,role,use_resume,request_id,goal_version
        FROM app.career_interview_preparations WHERE goal_id=%s ORDER BY created_at''', (goal_id,))


def last_step(owner, goal_id):
    return query(owner, '''SELECT id,message,input_payload FROM app.career_steps
        WHERE goal_id=%s ORDER BY goal_version DESC,cycle DESC LIMIT 1''', (goal_id,))[0]


def view(client, goal):
    return client.get('/api/career-goals/' + goal['id']).json()


def test_explicit_training_turn_creates_exactly_one_preparation(career, worker_settings, pg_settings):
    client, _ = career
    goal = post(client, '/api/career-goals/', {
        'intent': 'training', 'request': 'training',
        'message': '帮我按这份 JD 练一场 Agent 开发面试。',
        'conditions': {'direction': 'AI 应用开发'}}).json()
    arun(process(worker_settings, goal['id']))

    current = view(client, goal)
    assert current['status'] == 'WAITING_FOR_TOOL'
    assert current['action']['type'] == 'prepare_training'
    rows = preps(pg_settings[1], goal['id'])
    assert len(rows) == 1
    assert rows[0]['status'] == 'PENDING' and rows[0]['role'] == 'ai-application'
    assert rows[0]['use_resume'] is True and rows[0]['goal_version'] == 1
    assert rows[0]['request_id'] == f"training-{last_step(pg_settings[1], goal['id'])['id']}"

    # The workbench reads it back by goal without holding a local id.
    read = client.get(f"/api/career-goals/{goal['id']}/interview-preparations/current")
    assert read.status_code == 200, read.text
    assert read.json()['id'] == str(rows[0]['id']) and read.json()['status'] == 'PENDING'


def test_saving_a_jd_alone_never_starts_a_preparation(career, worker_settings, pg_settings):
    client, _ = career
    goal = post(client, '/api/career-goals/', {
        'request': 'save_jd', 'message': '保存这份 JD 供后续使用。',
        'jd_text': '岗位要求：负责 Agent 工具链的 Python 服务开发。',
        'conditions': {'direction': 'AI 应用开发'}}).json()
    arun(process(worker_settings, goal['id']))

    current = view(client, goal)
    # The scripted coordinator still proposes training (a JD is a valid target), but a save is
    # not a request, so no task is created; the user decides explicitly.
    assert current['action']['type'] == 'prepare_training'
    assert preps(pg_settings[1], goal['id']) == []
    assert client.get(f"/api/career-goals/{goal['id']}/interview-preparations/current").status_code == 404


def test_an_ambiguous_role_asks_once_instead_of_guessing(career, worker_settings, pg_settings):
    client, _ = career
    goal = post(client, '/api/career-goals/', {
        'intent': 'training', 'request': 'training', 'message': '帮我练一场面试。',
        'jd_text': '岗位要求：熟悉 Python 或 Java，能写服务端接口。'}).json()
    arun(process(worker_settings, goal['id']))

    current = view(client, goal)
    assert current['status'] == 'WAITING_FOR_USER'
    assert current['action']['type'] == 'ask_user' and 'Java' in current['action']['text']
    assert current['action']['fields'] == {}
    assert preps(pg_settings[1], goal['id']) == []


def test_java_direction_prepares_a_java_role(career, worker_settings, pg_settings):
    client, _ = career
    goal = post(client, '/api/career-goals/', {
        'intent': 'training', 'request': 'training', 'message': '练一场 Java 后端面试。',
        'conditions': {'direction': 'Java后端'}}).json()
    arun(process(worker_settings, goal['id']))
    rows = preps(pg_settings[1], goal['id'])
    assert len(rows) == 1 and rows[0]['role'] == 'java-backend'


def test_an_explicit_no_resume_turn_reaches_the_preparation(career, worker_settings, pg_settings):
    client, _ = career
    goal = post(client, '/api/career-goals/', {
        'intent': 'training', 'request': 'training', 'use_resume': False,
        'message': '练一场 AI 应用面试，本场不用简历。',
        'conditions': {'direction': 'AI 应用开发'}}).json()
    arun(process(worker_settings, goal['id']))
    rows = preps(pg_settings[1], goal['id'])
    assert len(rows) == 1 and rows[0]['use_resume'] is False


def test_the_same_turn_replays_onto_the_same_preparation(career, worker_settings, pg_settings):
    client, _ = career
    goal = post(client, '/api/career-goals/', {
        'intent': 'training', 'request': 'training', 'message': '练一场 AI 应用面试。',
        'conditions': {'direction': 'AI 应用开发'}}).json()
    arun(process(worker_settings, goal['id']))
    row = preps(pg_settings[1], goal['id'])[0]
    key = row['request_id']
    owner_id = str(query(pg_settings[1], 'SELECT user_id FROM app.career_goals WHERE id=%s',
                         (goal['id'],))[0]['user_id'])

    async def replay():
        async with resources(worker_settings) as (old, _queue):
            repo = PreparationRepository(old.pool)
            async with old.pool.connection() as conn, conn.transaction():
                same = await repo.create_at(
                    conn, owner_id, goal['id'], key,
                    CreatePreparation(expected_version=1, role='ai-application'))
                with pytest.raises(ApiError) as error:
                    await repo.create_at(conn, owner_id, goal['id'], key,
                                         CreatePreparation(expected_version=1, role='java-backend'))
                return same, error.value.code

    (identity, created), code = arun(replay())
    assert (identity, created, code) == (str(row['id']), False, 'idempotency_conflict')
    assert len(preps(pg_settings[1], goal['id'])) == 1
