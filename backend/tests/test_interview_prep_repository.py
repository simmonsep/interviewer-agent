"""S01: plan -> cross-process confirmation -> one idempotent session, through real PG/Redis."""
from uuid import uuid4

import pytest

from interview_agent.interview_prep import checkpointer
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.interview_prep.worker import worker_for
from interview_agent.interviews.contracts import CreateInterview
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.worker.queue import OutboxDispatcher
from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import resources, worker_settings
from test_material_repository import material_context  # noqa: F401  (pytest fixture used below)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

GOAL = {'message': '准备Java后端面试，无简历也可以。', 'conditions': {'direction': 'Java后端'}}


@pytest.fixture(scope='session')
def checkpoints(pg_settings):
    """The documented init command, run once per test session (idempotent)."""
    owner = pg_settings[1]
    return arun(checkpointer.initialize(owner, pg_settings[0].database.user))


@pytest.fixture
def context(pg_settings, checkpoints):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        uid = signup(client)['user']['id']
        goal = post(client, '/api/career-goals/', GOAL).json()
        yield client, goal, uid
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def post(client, path, body, key=None):
    return client.post(path, json=body, headers={'Idempotency-Key': key or uuid4().hex})


def base(goal):
    return f"/api/career-goals/{goal['id']}"


def create(client, goal, **body):
    return post(client, base(goal) + '/interview-preparations',
                {'expected_version': 1, 'role': 'java-backend', 'max_questions': 2, **body})


def confirm(client, goal, prep, decision='approve', key=None, **body):
    return post(client, base(goal) + f"/interview-preparations/{prep['id']}/confirm",
                {'expected_version': prep['version'], 'plan_sha256': prep['plan_sha256'],
                 'decision': decision, **body}, key)


def view(client, goal, prep_id):
    return client.get(base(goal) + f'/interview-preparations/{prep_id}').json()


async def once(settings):
    async with resources(settings) as (old, queue):
        repo = PreparationRepository(old.pool)
        await OutboxDispatcher(repo, queue).dispatch()
        worker = worker_for(repo, queue, pool=old.pool,
                            saver_factory=lambda: checkpointer.saver(settings.database))
        return await worker.once()


def wait_for_plan(client, goal, settings):
    prep = create(client, goal).json()
    assert arun(once(settings))
    return view(client, goal, prep['id'])


def execute(owner, statement, params=()):
    from psycopg import AsyncConnection

    async def run():
        async with await AsyncConnection.connect(owner.conninfo(), autocommit=True) as conn:
            return await conn.execute(statement, params)

    return arun(run())


def interviews(owner, goal_id):
    return query(owner, 'SELECT id,status FROM app.career_interviews WHERE goal_id=%s ORDER BY created_at',
                 (goal_id,))


def test_plan_then_approve_opens_exactly_one_session(context, worker_settings, pg_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    assert prep['status'] == 'WAITING_FOR_USER' and prep['plan']['version'] == 'prep-plan-v1'
    assert prep['plan']['role'] == 'java-backend' and prep['plan']['frozen']['goal_version'] == 1
    assert prep['session_id'] is None and len(prep['plan_sha256']) == 64

    approved = confirm(client, goal, prep).json()
    assert approved['status'] == 'PENDING' and approved['decision']['decision'] == 'approve'
    assert approved['version'] == prep['version'] + 1

    arun(once(worker_settings))
    done = view(client, goal, prep['id'])
    assert done['status'] == 'SUCCEEDED' and done['session_id']
    rows = interviews(pg_settings[1], goal['id'])
    assert len(rows) == 1 and str(rows[0]['id']) == done['session_id'] and rows[0]['status'] == 'PENDING'
    # The preparation only opens the session; the interview Worker still owns the questions.
    assert done['plan'] == prep['plan'] and done['decision']['decided_by'] == 'authenticated_user'


def test_confirm_replay_stale_plan_version_and_second_decision(context, worker_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    key = uuid4().hex
    first = confirm(client, goal, prep, key=key).json()
    replay = confirm(client, goal, prep, key=key)
    assert replay.status_code == 200 and replay.json()['version'] == first['version']

    other = confirm(client, goal, prep, decision='reject')
    assert other.status_code == 409 and other.json()['code'] == 'preparation_already_decided'

    arun(once(worker_settings))
    assert view(client, goal, prep['id'])['status'] == 'SUCCEEDED'

    second = wait_for_plan(client, goal, worker_settings)
    wrong_plan = confirm(client, goal, second, plan_sha256='c' * 64)
    assert wrong_plan.status_code == 409 and wrong_plan.json()['code'] == 'preparation_plan_changed'
    wrong_version = confirm(client, goal, second, expected_version=99)
    assert wrong_version.status_code == 409 and wrong_version.json()['code'] == 'version_conflict'


def test_another_account_cannot_read_or_confirm(context, worker_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    signup(client)  # replaces the session cookie with a different account
    assert client.get(base(goal) + f"/interview-preparations/{prep['id']}").status_code == 404
    assert confirm(client, goal, prep).status_code == 404


def test_reject_creates_nothing_and_a_second_preparation_is_allowed(context, worker_settings, pg_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    rejected = confirm(client, goal, prep, decision='reject', reason='not now').json()
    assert rejected['status'] == 'REJECTED' and rejected['decision']['reason'] == 'not now'
    arun(once(worker_settings))
    assert interviews(pg_settings[1], goal['id']) == []
    assert view(client, goal, prep['id'])['status'] == 'REJECTED'
    assert create(client, goal).status_code == 202


def test_only_one_preparation_can_be_active_per_goal(context, worker_settings):
    client, goal, _uid = context
    wait_for_plan(client, goal, worker_settings)
    blocked = create(client, goal)
    assert blocked.status_code == 409 and blocked.json()['code'] == 'preparation_in_progress'



def test_pause_written_but_status_lost_is_resynced_without_rerunning_the_graph(context, worker_settings, pg_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    # Crash window 1: the checkpoint holds the interrupt, the business row was never updated.
    execute(pg_settings[1], "UPDATE app.career_interview_preparations SET status='PENDING',plan=NULL,"
                            "plan_sha256=NULL,dispatched_at=NULL,attempt=0 WHERE id=%s", (prep['id'],))
    arun(once(worker_settings))
    recovered = view(client, goal, prep['id'])
    assert recovered['status'] == 'WAITING_FOR_USER'
    assert recovered['plan'] == prep['plan'] and recovered['plan_sha256'] == prep['plan_sha256']


def test_confirmed_but_not_dispatched_is_repicked_up(context, worker_settings, pg_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    confirm(client, goal, prep)
    # Crash window 2: the decision committed, the wake-up never went out.
    execute(pg_settings[1], 'UPDATE app.career_interview_preparations SET dispatched_at=NULL WHERE id=%s',
            (prep['id'],))
    arun(once(worker_settings))
    done = view(client, goal, prep['id'])
    assert done['status'] == 'SUCCEEDED' and done['session_id']
    assert len(interviews(pg_settings[1], goal['id'])) == 1


def test_the_stable_start_key_returns_the_same_session_when_replayed(context, worker_settings, pg_settings):
    client, goal, uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    confirm(client, goal, prep)
    arun(once(worker_settings))
    done = view(client, goal, prep['id'])

    async def replay():
        async with resources(worker_settings) as (old, _queue):
            repo = InterviewRepository(old.pool)
            value = CreateInterview(expected_version=1, role='java-backend', max_questions=2)
            first, created_first = await repo.create_for_owner(uid, goal['id'], f"prep-{prep['id']}", value)
            second, created_second = await repo.create_for_owner(uid, goal['id'], f"prep-{prep['id']}", value)
            return first, created_first, second, created_second

    first, created_first, second, created_second = arun(replay())
    assert first == second == done['session_id']
    assert (created_first, created_second) == (False, False)
    assert len(interviews(pg_settings[1], goal['id'])) == 1


def test_a_new_goal_version_marks_the_plan_stale(context, worker_settings, pg_settings):
    client, goal, _uid = context
    prep = wait_for_plan(client, goal, worker_settings)
    execute(pg_settings[1], 'UPDATE app.career_goals SET version=version+1 WHERE id=%s', (goal['id'],))
    stale = view(client, goal, prep['id'])
    assert stale['status'] == 'STALE' and stale['error_code'] == 'context_changed'
    response = confirm(client, goal, stale)
    assert response.status_code == 409 and response.json()['code'] == 'preparation_not_awaiting_confirmation'


def test_the_preparation_path_spends_nothing(context, worker_settings, pg_settings):
    client, goal, _uid = context
    before = query(pg_settings[1], 'SELECT count(*) AS n FROM app.model_http_calls')[0]['n']
    prep = wait_for_plan(client, goal, worker_settings)
    confirm(client, goal, prep)
    arun(once(worker_settings))
    after = query(pg_settings[1], 'SELECT count(*) AS n FROM app.model_http_calls')[0]['n']
    assert before == after
    assert view(client, goal, prep['id'])['status'] == 'SUCCEEDED'


def test_current_preparation_is_goal_scoped_and_needs_no_local_id(context, worker_settings):
    c, g, _ = context
    assert c.get(base(g) + '/interview-preparations/current').status_code == 404
    prep = wait_for_plan(c, g, worker_settings)
    current = c.get(base(g) + '/interview-preparations/current')
    assert current.status_code == 200, current.text
    assert current.json()['id'] == prep['id'] and current.json()['status'] == 'WAITING_FOR_USER'
    other = post(c, '/api/career-goals/', GOAL).json()
    assert c.get(base(other) + '/interview-preparations/current').status_code == 404


def test_explicit_no_resume_skips_the_bound_draft_in_both_layers(material_context, worker_settings, pg_settings):
    """F07Z-166: a bound resume is a default, never something the user cannot turn off."""
    from test_material_repository import process as material_process, source, start, view as material_view
    from test_position_bindings import manual, position_id_for

    client, goal = material_context
    assert source(client, goal).status_code == 201
    assert start(client, goal, 1, mode='draft', draft_style='rewrite').status_code == 202
    arun(material_process(worker_settings))
    drafts = material_view(client, goal)['drafts']
    assert drafts, 'a rewrite draft is required for this test'
    latest = drafts[0]
    created = manual(client, goal)
    assert created.status_code == 201, created.text
    position_id = position_id_for(pg_settings, created.json()['snapshot_id'])
    binding = client.get(f"/api/career-goals/{goal['id']}/positions/{position_id}/binding").json()
    assert binding['active']['resume_draft_id'] == latest['id']

    prep = create(client, goal, use_resume=False).json()
    assert prep['use_resume'] is False and prep['draft_id'] is None
    assert prep['plan'] is None

    arun(once(worker_settings))
    planned = view(client, goal, prep['id'])
    assert planned['status'] == 'WAITING_FOR_USER'
    assert planned['plan']['draft'] is None and planned['plan']['use_resume'] is False
    assert planned['plan']['resume_item_indexes'] == []

    confirm(client, goal, planned)
    arun(once(worker_settings))
    done = view(client, goal, planned['id'])
    assert done['status'] == 'SUCCEEDED' and done['session_id']
    payload = query(pg_settings[1], 'SELECT input_payload FROM app.career_interviews WHERE id=%s',
                    (done['session_id'],))[0]['input_payload']
    assert payload['draft_id'] is None and payload['facts'] == []


@pytest.fixture
def ambiguous_goal(pg_settings):
    """A goal with no direction and no JD: the role cannot be determined."""
    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        uid = signup(client)['user']['id']
        goal = post(client, '/api/career-goals/', {'message': '帮我练一场面试。'}).json()
        yield client, goal
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def test_role_is_resolved_from_the_goal_when_omitted(context):
    """F07Z-166 D2: the workbench no longer sends a hard-coded role."""
    client, goal, _uid = context
    response = post(client, base(goal) + '/interview-preparations',
                    {'expected_version': 1, 'max_questions': 2})
    assert response.status_code == 202, response.text
    assert response.json()['role'] == 'java-backend'


def test_role_is_required_when_the_goal_does_not_determine_it(ambiguous_goal):
    client, goal = ambiguous_goal
    response = post(client, base(goal) + '/interview-preparations', {'expected_version': 1})
    assert response.status_code == 409 and response.json()['code'] == 'role_required'


def test_a_direct_interview_without_role_uses_the_goal_direction(context):
    client, goal, _uid = context
    response = post(client, base(goal) + '/interviews', {'expected_version': 1, 'max_questions': 2})
    assert response.status_code == 202, response.text
    assert response.json()['policy']['role'] == 'java-backend'


def test_a_direct_interview_requires_a_resolvable_role(ambiguous_goal):
    client, goal = ambiguous_goal
    response = post(client, base(goal) + '/interviews', {'expected_version': 1})
    assert response.status_code == 409 and response.json()['code'] == 'role_required'

