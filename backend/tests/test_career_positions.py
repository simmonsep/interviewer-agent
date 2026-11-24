"""F07Z-152 phase 2.1: a position id that does not change with the JD text."""
from uuid import uuid4

import pytest

from test_postgres import pg_settings, query  # noqa: F401  (fixture reuse)


def create_goal(client):
    response = client.post('/api/career-goals/', headers={'Idempotency-Key': uuid4().hex},
                           json={'message': '准备Java后端面试，无简历也可以。',
                                 'conditions': {'direction': 'Java后端'}})
    assert response.status_code == 202, response.text
    return response.json()


def manual(client, goal, jd='负责Java服务开发，写接口与事务。', title='Java实习'):
    return client.post(f"/api/career-goals/{goal['id']}/jobs/manual",
                       headers={'Idempotency-Key': uuid4().hex},
                       json={'expected_version': 1, 'company': '合成公司', 'title': title,
                             'cities': ['上海'], 'jd_text': jd})


@pytest.mark.postgres
def test_a_manual_job_gets_one_position_and_the_snapshot_points_at_it(pg_settings):
    from test_postgres import client_for, signup

    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = signup(client)['user']['id']
        goal = create_goal(client)
        first = manual(client, goal)
        assert first.status_code == 201, first.text
        snapshot_id = first.json()['snapshot_id']

        rows = query(pg_settings[1], '''SELECT p.id AS position_id, p.current_snapshot_id,
                s.position_id AS snapshot_position, p.source_id, p.source_job_id
            FROM app.career_job_snapshots s
            JOIN app.career_positions p ON p.goal_id=s.goal_id AND p.source_id=s.source_id
             AND p.source_job_id=s.source_job_id
            WHERE s.id=%s''', (snapshot_id,))
        assert len(rows) == 1
        row = rows[0]
        assert str(row['snapshot_position']) == str(row['position_id'])
        assert str(row['current_snapshot_id']) == snapshot_id
        assert row['source_id'] == 'user-jd'

        # Saving the identical job again reuses the snapshot and the position.
        again = manual(client, goal)
        assert again.status_code == 200 and again.json()['snapshot_id'] == snapshot_id
        counts = query(pg_settings[1], '''SELECT
                (SELECT count(*) FROM app.career_positions WHERE goal_id=%s) AS positions,
                (SELECT count(*) FROM app.career_job_snapshots WHERE goal_id=%s) AS snapshots''',
                       (goal['id'], goal['id']))[0]
        assert counts == {'positions': 1, 'snapshots': 1}

        # A genuinely different job in the same goal gets its own position.
        other = manual(client, goal, jd='负责前端页面与组件开发。', title='前端实习')
        assert other.status_code == 201
        positions = query(pg_settings[1],
                          'SELECT count(*) AS n FROM app.career_positions WHERE goal_id=%s', (goal['id'],))[0]
        assert positions['n'] == 2
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))


@pytest.mark.postgres
def test_editing_the_jd_keeps_the_same_position_and_appends_a_snapshot(pg_settings):
    from test_postgres import client_for, signup

    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = signup(client)['user']['id']
        goal = create_goal(client)
        first = manual(client, goal)
        assert first.status_code == 201
        first_snapshot = first.json()['snapshot_id']
        position = query(pg_settings[1], '''SELECT p.id,p.version,s.content_sha256 FROM app.career_positions p
            JOIN app.career_job_snapshots s ON s.position_id=p.id
            WHERE s.id=%s''', (first_snapshot,))[0]
        position_id = str(position['id'])
        old_hash = position['content_sha256']
        assert position['version'] == 1

        def revise(jd, expected):
            return client.post(f"/api/career-goals/{goal['id']}/jobs/{position_id}/revisions",
                               headers={'Idempotency-Key': uuid4().hex},
                               json={'expected_version': expected, 'company': '合成公司',
                                     'title': 'Java实习', 'cities': ['上海'], 'jd_text': jd})

        updated = revise('负责Java服务开发，写接口、事务与幂等。', 1)
        assert updated.status_code == 201, updated.text
        second_snapshot = updated.json()['snapshot_id']
        assert second_snapshot != first_snapshot
        state = query(pg_settings[1], '''SELECT p.id,p.version,p.current_snapshot_id,
                (SELECT count(*) FROM app.career_job_snapshots WHERE position_id=p.id) AS snapshots
            FROM app.career_positions p WHERE p.id=%s''', (position_id,))[0]
        assert str(state['id']) == position_id
        assert state['version'] == 2
        assert str(state['current_snapshot_id']) == second_snapshot
        assert state['snapshots'] == 2
        # The old snapshot is still there, unchanged.
        old = query(pg_settings[1], 'SELECT content_sha256 FROM app.career_job_snapshots WHERE id=%s',
                    (first_snapshot,))[0]
        assert old['content_sha256'] == old_hash

        # Identical content is a no-op.
        same = revise('负责Java服务开发，写接口、事务与幂等。', 2)
        assert same.status_code == 200 and same.json()['snapshot_id'] == second_snapshot
        assert query(pg_settings[1], 'SELECT version FROM app.career_positions WHERE id=%s',
                     (position_id,))[0]['version'] == 2

        # A stale position version is rejected, not silently applied.
        stale = revise('另一个版本。', 1)
        assert stale.status_code == 409 and stale.json()['code'] == 'position_version_conflict'

        # Restoring the previous text reuses the old snapshot and moves the pointer back.
        restored = revise('负责Java服务开发，写接口与事务。', 2)
        assert restored.status_code == 201, restored.text
        assert restored.json()['snapshot_id'] == first_snapshot
        assert query(pg_settings[1], 'SELECT version FROM app.career_positions WHERE id=%s',
                     (position_id,))[0]['version'] == 3
        assert query(pg_settings[1], 'SELECT count(*) AS n FROM app.career_job_snapshots WHERE position_id=%s',
                     (position_id,))[0]['n'] == 2
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))


@pytest.mark.postgres
def test_no_snapshot_is_left_without_a_position(pg_settings):
    missing = query(pg_settings[1],
                    'SELECT count(*) AS n FROM app.career_job_snapshots WHERE position_id IS NULL')[0]
    assert missing['n'] == 0, 'the migration must backfill every snapshot'
