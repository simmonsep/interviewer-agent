"""F07Z-152 phase 2.3: each position binds one resume version, and new drafts do not follow."""
from uuid import uuid4

import pytest

from test_material_repository import material_context, source, start, process, view  # noqa: F401
from test_postgres import arun, pg_settings, query  # noqa: F401
from test_worker import worker_settings  # noqa: F401


def manual(client, goal, jd='负责Java服务开发，写接口与事务。'):
    return client.post(f"/api/career-goals/{goal['id']}/jobs/manual",
                       headers={'Idempotency-Key': uuid4().hex},
                       json={'expected_version': 1, 'company': '合成公司', 'title': 'Java实习',
                             'cities': ['上海'], 'jd_text': jd})


def position_id_for(pg_settings, snapshot_id):
    return str(query(pg_settings[1],
                     'SELECT position_id FROM app.career_job_snapshots WHERE id=%s', (snapshot_id,))[0]['position_id'])


@pytest.mark.postgres
@pytest.mark.redis
def test_a_new_position_binds_the_latest_resume_and_never_follows_new_ones(material_context, pg_settings, worker_settings):
    client, goal = material_context
    assert source(client, goal).status_code == 201
    first = start(client, goal, 1, mode='draft', draft_style='rewrite')
    assert first.status_code == 202
    arun(process(worker_settings))
    drafts = view(client, goal)['drafts']
    assert drafts, 'a rewrite draft is required for this test'
    latest = drafts[0]
    assert latest['payload']['authoring_record']['origin'] == 'agent_rewrite'
    assert latest['payload']['authoring_record']['version'] == latest['version']

    created = manual(client, goal)
    assert created.status_code == 201, created.text
    position_id = position_id_for(pg_settings, created.json()['snapshot_id'])

    binding = client.get(f"/api/career-goals/{goal['id']}/positions/{position_id}/binding").json()
    assert binding['active']['resume_draft_id'] == latest['id']
    assert binding['active']['resume_version'] == latest['version']
    assert binding['position_version'] == 1

    # Re-selecting the same draft is a no-op, not a new history row.
    same = client.put(f"/api/career-goals/{goal['id']}/positions/{position_id}/binding",
                      headers={'Idempotency-Key': uuid4().hex},
                      json={'expected_version': 1, 'draft_id': latest['id']})
    assert same.status_code == 200
    assert same.json()['position_version'] == 1

    # A second draft does not move an existing binding.
    second = start(client, goal, 1, mode='draft', draft_style='rewrite')
    assert second.status_code == 202
    arun(process(worker_settings))
    newer = view(client, goal)['drafts'][0]
    assert newer['id'] != latest['id'] and newer['version'] > latest['version']
    still = client.get(f"/api/career-goals/{goal['id']}/positions/{position_id}/binding").json()
    assert still['active']['resume_draft_id'] == latest['id']

    # Rebinding is explicit, versioned and keeps history.
    rebound = client.put(f"/api/career-goals/{goal['id']}/positions/{position_id}/binding",
                         headers={'Idempotency-Key': uuid4().hex},
                         json={'expected_version': 1, 'draft_id': newer['id']})
    assert rebound.status_code == 201, rebound.text
    assert rebound.json()['active']['resume_draft_id'] == newer['id']
    assert rebound.json()['position_version'] == 2
    assert len(rebound.json()['history']) == 2

    stale = client.put(f"/api/career-goals/{goal['id']}/positions/{position_id}/binding",
                       headers={'Idempotency-Key': uuid4().hex},
                       json={'expected_version': 1, 'draft_id': latest['id']})
    assert stale.status_code == 409 and stale.json()['code'] == 'position_version_conflict'

    # Selecting the position and opening a session without a draft_id uses the bound version.
    created_body = created.json()
    selected = client.post(f"/api/career-goals/{goal['id']}/selection",
                           headers={'Idempotency-Key': uuid4().hex},
                           json={'expected_version': 1, 'run_id': created_body['runs'][0]['id'],
                                 'snapshot_id': created_body['snapshot_id'], 'acknowledge_unknown': True})
    assert selected.status_code == 200, selected.text
    session = client.post(f"/api/career-goals/{goal['id']}/interviews",
                          headers={'Idempotency-Key': uuid4().hex},
                          json={'expected_version': 1, 'role': 'java-backend', 'max_questions': 2})
    assert session.status_code == 202, session.text
    frozen = query(pg_settings[1], 'SELECT input_payload FROM app.career_interviews WHERE id=%s',
                   (session.json()['id'],))[0]['input_payload']
    assert frozen['draft_id'] == newer['id']
    assert frozen['facts'], 'the bound resume contributes project anchors'

    timeline_response = client.get(f"/api/career-goals/{goal['id']}/timeline")
    assert timeline_response.status_code == 200, timeline_response.text
    timeline = timeline_response.json()['items']
    kinds = {item['type'] for item in timeline}
    assert {'resume_version', 'position_binding', 'interview_session'} <= kinds
    times = [item['at'] for item in timeline]
    assert times == sorted(times, reverse=True)
    assert not any('observation' in item for item in timeline)
