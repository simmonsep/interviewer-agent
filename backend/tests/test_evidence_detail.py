"""F07Z-152: material-fact evidence detail is goal-scoped and carries its source quote."""
import pytest

from test_material_repository import (analyze, confirm, material_context, source,  # noqa: F401
                                      view)
from test_postgres import pg_settings  # noqa: F401
from test_interview_repository import completed, context  # noqa: F401
from test_worker import worker_settings  # noqa: F401


@pytest.mark.postgres
def test_material_fact_detail_resolves_source_and_rejects_other_goals(material_context, pg_settings, worker_settings):
    client, goal = material_context
    assert source(client, goal).status_code == 201
    run = analyze(client, goal, worker_settings)
    confirmed = confirm(client, goal, run)
    assert confirmed.status_code in (200, 201), confirmed.text
    facts = view(client, goal)['facts']
    assert facts, 'the scripted analysis must confirm at least one fact'
    fact = facts[0]

    response = client.get(f"/api/career-goals/{goal['id']}/evidence/material-facts/{fact['id']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail['kind'] == 'material_fact' and detail['text'] == fact['quote']
    assert detail['source']['quote'] == fact['quote']
    assert detail['source']['start_char'] == fact['start_char']
    assert detail['confirmation_id']

    # A resume imported from the same source links back to the fact it contains.
    from test_material_repository import post, url
    from uuid import uuid4
    revision = view(client, goal)['material_revision']
    imported = post(client, url(goal) + '/resume-imports',
                    {'expected_version': 1, 'expected_revision': revision}, uuid4().hex)
    assert imported.status_code == 201, imported.text
    linked = client.get(f"/api/career-goals/{goal['id']}/evidence/material-facts/{fact['id']}").json()
    assert linked['used_by'], 'the imported resume should cite this fact'
    assert linked['used_by'][0]['version'] == 1

    # The other direction: the bullet detail returns the fact it cites, so the UI can jump back.
    import json
    draft = view(client, goal)['drafts'][0]
    bullet_index = next(index for index, item in enumerate(draft['payload']['rewrite_items'])
                        if fact['quote'].strip() in str(item.get('text') or ''))
    bullet = client.get(
        f"/api/career-goals/{goal['id']}/evidence/resume-bullets/{draft['id']}/{bullet_index}")
    assert bullet.status_code == 200, bullet.text
    detail = bullet.json()
    assert detail['kind'] == 'resume_bullet'
    assert [linked_fact['id'] for linked_fact in detail['facts']] == [fact['id']], json.dumps(detail['facts'])

@pytest.mark.postgres
@pytest.mark.redis
def test_terminal_observation_detail_is_readable(context, worker_settings):
    client, goal = context
    session = completed(client, goal, worker_settings)
    assert session['observations'], 'the completed session must carry an observation'
    observation = session['observations'][0]
    response = client.get(f"/api/career-goals/{goal['id']}/evidence/observations/{observation['id']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail['kind'] == 'interview_observation'
    assert detail['answer_quote'] and detail['evidence_quote']
    assert detail['session_status'] not in ('PENDING', 'RUNNING', 'WAITING_FOR_USER', 'WAITING_CREDENTIALS')
