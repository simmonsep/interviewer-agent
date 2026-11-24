"""S05 follow-up: cross-session retrieval A/B, measured on the real read path.

S02's acceptance table asks one question that had no harness: with the same frozen goal and
role, does a coaching session start from the *topic-relevant* older observation, or from
whatever happens to be recent? The product has two arms and this file runs both:

- **A**: `InterviewRepository` without a projector -> `ORDER BY created_at DESC LIMIT 6`;
- **B**: the same repository with `MemoryProjector` -> Store topic candidates, then the same
  SQL re-validation (existence, completion, role, correction).

Both arms run against real PostgreSQL and the real LangGraph Store. No model is called, so the
measurement costs 0. The corpus is `benchmarks/interview-recall-v0.1/cases.jsonl`; the fixture
rows are inserted with a controlled age so "older but relevant" is a fact, not a hope.
"""
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from psycopg.types.json import Jsonb

from interview_agent.interviews.memory_store import MemoryProjector
from interview_agent.interviews.repository import InterviewRepository
from test_interview_repository import context, post  # noqa: F401  (fixture: client + goal)
from test_postgres import arun, pg_settings, query  # noqa: F401  (fixtures)
from test_worker import resources, worker_settings  # noqa: F401  (fixture + pool helper)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'benchmarks' / 'interview-recall-v0.1'
ROLE = 'java-backend'


def load():
    manifest = json.loads((CORPUS / 'manifest.json').read_text(encoding='utf-8'))
    body = (CORPUS / 'cases.jsonl').read_bytes()
    assert hashlib.sha256(body).hexdigest() == manifest['sha256']['cases.jsonl'], 'corpus_changed'
    return [json.loads(line) for line in body.decode('utf-8').splitlines() if line.strip()]


def identity(case_id, key):
    """Stable uuid per (case, key): the fixture is reproducible across runs."""
    return str(uuid5(NAMESPACE_URL, f'interview-recall-v0.1/{case_id}/{key}'))


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


async def seed(conn, goal_id, goal_version, case):
    """One COMPLETED coaching session per observation, with a controlled age.

    Rows are inserted *without* corrections first: case R04 needs the projection to hold a
    key that is retracted afterwards, which is the only way to observe the stale-hit path.
    """
    sessions = {}
    for item in case['observations']:
        session_id = identity(case['case_id'], 'session/' + item['key'])
        answer_id = identity(case['case_id'], 'answer/' + item['key'])
        await conn.execute(
            '''INSERT INTO app.career_interviews
               (id,goal_id,goal_version,material_revision,memory_revision,request_id,request_hash,
                input_payload,status,deadline_at,created_at)
               VALUES (%s,%s,%s,0,0,%s,%s,%s,'COMPLETED',clock_timestamp(),
                       clock_timestamp()-(%s*interval '1 hour'))''',
            (session_id, goal_id, goal_version, 'recall-' + case['case_id'] + '-' + item['key'],
             digest(case['case_id'] + item['key']),
             Jsonb({'policy': {'role': ROLE, 'mode': 'coaching'}, 'application': None}),
             item['age_hours']))
        await conn.execute(
            '''INSERT INTO app.career_interview_events(id,session_id,seq,kind,payload,created_at)
               VALUES (%s,%s,1,'answer',%s,clock_timestamp()-(%s*interval '1 hour'))''',
            (answer_id, session_id, Jsonb({'text': '第 ' + item['key'] + ' 场的原始回答。'}),
             item['age_hours']))
        await conn.execute(
            '''INSERT INTO app.career_interview_observations
               (id,goal_id,session_id,answer_id,payload,created_at)
               VALUES (%s,%s,%s,%s,%s,clock_timestamp()-(%s*interval '1 hour'))''',
            (identity(case['case_id'], 'observation/' + item['key']), goal_id, session_id, answer_id,
             Jsonb({'answer_quote': '第 ' + item['key'] + ' 场的原答节选。', 'pack_version': 'recall-v1',
                    'topic_id': item['topic_id'], 'label': item['label'], 'summary': '需要复测的点。',
                    'assisted': item['assisted'], 'origin': 'scripted_protocol'}),
             item['age_hours']))
        sessions[item['key']] = (session_id, identity(case['case_id'], 'observation/' + item['key']))
    return sessions


async def retract(conn, goal_id, correction):
    """Retract results, the way `POST /interview-memory/{id}/retract` does."""
    for key, item in correction.items():
        await conn.execute(
            '''INSERT INTO app.career_interview_corrections
               (goal_id,observation_id,request_id,request_hash,reason)
               VALUES (%s,%s,%s,%s,%s)''',
            (goal_id, item['observation_id'], 'recall-retract-' + key, digest('retract' + key),
             '这条观察需要更正。'))


async def read(repo, conn, goal_id, user_id, topics):
    rows = await repo._prior_observations(conn, goal_id, user_id, ROLE, 'coaching',
                                          {'topic_order': list(topics)})
    return [str(row['id']) for row in rows]


def measure(owner, database, worker, goal, case):
    """Run both arms on one case and return their observation ids."""
    # query owns its event loop, so the goal row is read before the scenario starts.
    info = query(owner, 'SELECT user_id,version FROM app.career_goals WHERE id=%s', (goal['id'],))[0]

    async def scenario():
        async with resources(worker) as (pool_repo, _queue):
            async with pool_repo.pool.connection() as conn:
                sessions = await seed(conn, goal['id'], info['version'], case)
            projector = MemoryProjector(database)
            for session_id, _observations in sessions.values():
                await projector.project_session(pool_repo.pool, session_id)
            corrected = {item['key']: {'observation_id': sessions[item['key']][1]}
                         for item in case['observations'] if item['correction']}
            if corrected:
                async with pool_repo.pool.connection() as conn:
                    await retract(conn, goal['id'], corrected)
            plain = InterviewRepository(pool_repo.pool)
            indexed = InterviewRepository(pool_repo.pool, projector=projector)
            async with pool_repo.pool.connection() as conn:
                sql = await read(plain, conn, goal['id'], info['user_id'], case['pack_topics'])
                before = await read(indexed, conn, goal['id'], info['user_id'], case['pack_topics'])
            if corrected:
                # The live path re-projects on write; here that is the operator's backfill.
                for key in corrected:
                    await projector.project_session(pool_repo.pool, sessions[key][0])
                async with pool_repo.pool.connection() as conn:
                    after = await read(indexed, conn, goal['id'], info['user_id'], case['pack_topics'])
            else:
                after = before
            keys = {observation_id: key for key, (_session, observation_id) in sessions.items()}
            # Query order is part of the contract: created_at DESC after re-validation.
            as_keys = lambda ids: [keys[i] for i in ids]
            return as_keys(sql), as_keys(before), as_keys(after)

    return arun(scenario())


def test_topic_retrieval_beats_plain_recency_and_never_leaks_a_retracted_row(
        context, worker_settings, pg_settings):
    client, _unused = context  # the fixture signs one user up and creates one goal
    cases = load()
    assert len(cases) == 4
    measured = []
    for case in cases:
        # One goal per case: the retrieval query is scoped by goal, and a shared goal would
        # let one case's rows crowd out the next case's window (that is what "recency" means).
        goal = post(client, '/api/career-goals/',
                    {'message': '准备Java后端面试，无简历也可以。',
                     'conditions': {'direction': 'Java后端'}}).json()
        sql, before, after = measure(pg_settings[1], pg_settings[0].database, worker_settings, goal, case)
        measured.append({'case': case['case_id'], 'shape': case['shape'],
                         'sql': sql, 'store': before, 'store_after_reprojection': after})
        assert sql == case['expected_sql'], (case['case_id'], 'sql', sql)
        assert before == case['expected_store'], (case['case_id'], 'store', before)
        expected_after = case.get('expected_store_after_reprojection', case['expected_store'])
        assert after == expected_after, (case['case_id'], 'store_after', after)

    # The point of the A/B, stated as denominators rather than adjectives.
    for case, row in zip(cases, measured):
        relevant = set(case['relevant_keys'])
        if not relevant:
            continue
        sql_recall = len(relevant & set(row['sql'])) / len(relevant)
        store_recall = len(relevant & set(row['store_after_reprojection'])) / len(relevant)
        assert store_recall >= sql_recall, (case['case_id'], sql_recall, store_recall)
    print(json.dumps(measured, ensure_ascii=False, indent=1))
