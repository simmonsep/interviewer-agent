"""F07Z-152 phase 1: the uploaded text becomes resume v1 with zero model calls."""
from uuid import uuid4

import pytest

from test_material_repository import material_context, post, source, url, view  # noqa: F401
from test_postgres import pg_settings, query  # noqa: F401
from test_worker import worker_settings  # noqa: F401


@pytest.mark.postgres
def test_import_resume_is_immutable_versioned_and_never_calls_a_model(material_context, pg_settings):
    client, goal = material_context
    assert source(client, goal).status_code == 201

    key = uuid4().hex
    first = post(client, url(goal) + '/resume-imports',
                 {'expected_version': 1, 'expected_revision': 1}, key)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body['version'] == 1
    draft_id = body['draft_id']

    drafts = view(client, goal)['drafts']
    assert drafts[0]['id'] == draft_id
    payload = drafts[0]['payload']
    assert payload['draft_style'] == 'verbatim'
    assert payload['authoring_record']['origin'] == 'uploaded'
    assert payload['authoring_record']['items']
    assert all(item['change_note'] == '按原文导入' for item in payload['rewrite_items'])
    assert drafts[0]['markdown'].strip().startswith('- ')

    bullet = client.get(f"/api/career-goals/{goal['id']}/evidence/resume-bullets/{draft_id}/0")
    assert bullet.status_code == 200, bullet.text
    detail = bullet.json()
    assert detail['kind'] == 'resume_bullet' and detail['origin'] == 'uploaded'
    assert detail['text'] and detail['segments'] and detail['sources']
    assert detail['flagged'] is False

    # No model run was created for this version.
    runs = query(pg_settings[1], 'SELECT count(*) AS n FROM app.career_material_runs WHERE goal_id=%s',
                 (goal['id'],))[0]['n']
    assert runs == 0

    replay = post(client, url(goal) + '/resume-imports',
                  {'expected_version': 1, 'expected_revision': 1}, key)
    assert replay.status_code == 200 and replay.json()['draft_id'] == draft_id

    second = post(client, url(goal) + '/resume-imports',
                  {'expected_version': 1, 'expected_revision': 1}, uuid4().hex)
    assert second.status_code == 201 and second.json()['version'] == 2

    # The first version stays byte-identical.
    old = query(pg_settings[1], 'SELECT payload,markdown FROM app.career_material_drafts WHERE id=%s',
                (draft_id,))[0]
    assert old['payload']['authoring_record']['version'] == 1

    # Restoring the first version appends a new one; history is untouched.
    revision = view(client, goal)['material_revision']
    restored = post(client, url(goal) + f'/drafts/{draft_id}/restore',
                    {'expected_version': 1, 'expected_revision': revision}, uuid4().hex)
    assert restored.status_code == 201, restored.text
    newest = view(client, goal)['drafts'][0]
    assert newest['version'] == 3 and newest['origin'] == 'restore'
    assert newest['markdown'] == drafts[0]['markdown']
    record = newest['payload']['authoring_record']
    assert record['restored_from_version'] == 1 and record['parent_version'] == 2



def test_usable_markdown_drops_flagged_or_unsourced_items():
    from interview_agent.materials.usable import usable_markdown, usable_items

    payload = {'rewrite_items': [
        {'text': '负责缓存改造。', 'source_ids': ['s1'], 'evidence_segments': ['S001'], 'change_note': '导入'},
        {'text': '主导整个系统。', 'source_ids': ['s1'], 'evidence_segments': ['S002'], 'change_note': '改写'},
        {'text': '没有出处的句子。', 'source_ids': [], 'evidence_segments': [], 'change_note': ''},
    ], 'grounding_findings': [{'text': '主导整个系统。', 'code': 'responsibility'}]}
    kept = usable_items(payload)
    assert [item['text'] for item in kept] == ['负责缓存改造。']
    assert '负责缓存改造' in usable_markdown(payload)
    assert '主导整个系统' not in usable_markdown(payload)


@pytest.mark.postgres
def test_imported_verbatim_resume_can_open_an_interview_and_an_application(material_context, pg_settings):
    """A bound verbatim draft has no confirmed facts; creating a session or package must not 500."""
    client, goal = material_context
    assert source(client, goal).status_code == 201
    imported = post(client, url(goal) + '/resume-imports',
                    {'expected_version': 1, 'expected_revision': 1}, uuid4().hex)
    assert imported.status_code == 201, imported.text
    draft_id = imported.json()['draft_id']

    created = client.post(f"/api/career-goals/{goal['id']}/jobs/manual",
                          headers={'Idempotency-Key': uuid4().hex},
                          json={'expected_version': 1, 'company': '合成公司', 'title': 'Java实习',
                                'cities': ['上海'], 'jd_text': '负责Java服务开发，写接口与事务。'})
    assert created.status_code == 201, created.text
    body = created.json()
    selected = client.post(f"/api/career-goals/{goal['id']}/selection",
                           headers={'Idempotency-Key': uuid4().hex},
                           json={'expected_version': 1, 'run_id': body['runs'][0]['id'],
                                 'snapshot_id': body['snapshot_id'], 'acknowledge_unknown': True})
    assert selected.status_code == 200, selected.text

    session = client.post(f"/api/career-goals/{goal['id']}/interviews",
                          headers={'Idempotency-Key': uuid4().hex},
                          json={'expected_version': 1, 'role': 'java-backend', 'max_questions': 2})
    assert session.status_code == 202, session.text
    frozen = query(pg_settings[1], 'SELECT input_payload FROM app.career_interviews WHERE id=%s',
                   (session.json()['id'],))[0]['input_payload']
    assert frozen['draft_id'] == draft_id
    # The uploaded lines now anchor questions, still marked as user-selected, not confirmed facts.
    assert frozen['facts'] and all(f.get('resume_source') for f in frozen['facts'])
    assert all(f['verification'] == 'user_selected_resume_not_external_verified' for f in frozen['facts'])

    application = client.post(f"/api/career-goals/{goal['id']}/applications",
                              headers={'Idempotency-Key': uuid4().hex},
                              json={'expected_version': 1, 'expected_material_revision': 1,
                                    'company': '合成公司', 'title': 'Java实习'})
    assert application.status_code == 201, application.text
    package = query(pg_settings[1],
                    'SELECT payload FROM app.career_application_packages WHERE application_id=%s',
                    (application.json()['id'],))[0]['payload']
    assert package['material']['kind'] == 'draft'
    assert package['material']['facts'] == []
    assert package['material']['resume_evidence']

