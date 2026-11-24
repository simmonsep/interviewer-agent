"""F07Z-164: user-imported project follow-up strategy cards (guidance, never questions)."""
from uuid import uuid4

import pytest

from interview_agent.storage.errors import ApiError
from interview_agent.strategies.install import parse_document

from test_postgres import pg_settings  # noqa: F401  (fixture reuse)


def document(cards=None, role='java-backend', name='我的项目追问'):
    return {'schema_version': 1, 'id': 'my-project-probes', 'name': name, 'role': role,
            'cards': cards if cards is not None else [{
                'id': 'my-lock-probe', 'title': '锁的失效边界',
                'applies_to': ['Redis', '锁'], 'trigger': ['锁', '过期'],
                'dimensions': ['verification', 'failure'],
                'example_question': '锁过期后旧执行者还在跑，你会怎样验证不会覆盖新持有者？'}]}


def parse_error(code, value):
    with pytest.raises(ApiError) as error:
        parse_document(value)
    assert error.value.code == code, error.value.code


def test_a_valid_document_is_normalized():
    parsed = parse_document(document())
    assert parsed['role'] == 'java-backend' and parsed['name'] == '我的项目追问'
    assert parsed['cards'][0]['id'] == 'my-lock-probe'
    assert parsed['source'] == 'user_imported_declarative'


def test_the_document_is_validated():
    parse_error('strategy_document_invalid', 'x')
    parse_error('strategy_schema_version_unsupported', {**document(), 'schema_version': 2})
    parse_error('strategy_role_invalid', document(role='frontend'))
    parse_error('strategy_set_id_invalid', {**document(), 'id': 'Bad Id'})
    parse_error('strategy_name_invalid', {**document(), 'name': ''})
    parse_error('strategy_cards_invalid', document(cards=[]))
    parse_error('strategy_id_invalid', document(cards=[{'id': 'Bad Id', 'title': 'x'}]))
    parse_error('strategy_title_invalid', document(cards=[{'id': 'ok-id', 'title': ''}]))
    parse_error('strategy_dimensions_invalid',
                document(cards=[{'id': 'ok-id', 'title': 'x', 'dimensions': ['score']}]))
    parse_error('strategy_id_duplicate',
                document(cards=[{'id': 'ok-id', 'title': 'a'}, {'id': 'ok-id', 'title': 'b'}]))


def test_a_user_card_cannot_shadow_a_preinstalled_id():
    parse_error('strategy_id_conflicts_builtin',
                document(cards=[{'id': 'lock-verification', 'title': '同名'}]))
    # The same id is fine for the other role, where it does not exist.
    parse_document(document(cards=[{'id': 'lock-verification', 'title': '同名'}], role='ai-application'))


@pytest.mark.postgres
def test_import_versions_enable_and_freeze_the_cards(pg_settings):
    from test_postgres import client_for, query, signup
    import test_interview_repository as helpers

    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = signup(client)['user']['id']
        preview = client.post('/api/strategies/imports', headers={'Idempotency-Key': uuid4().hex},
                              json={'document': document()})
        assert preview.status_code == 201, preview.text
        preview_id = preview.json()['preview_id']
        first = client.post(f'/api/strategies/imports/{preview_id}/commit',
                            headers={'Idempotency-Key': uuid4().hex}, json={'enabled': True})
        assert first.status_code == 201, first.text
        assert first.json()['version'] == 1 and first.json()['enabled'] is True

        second = client.post(f'/api/strategies/imports/{preview_id}/commit',
                             headers={'Idempotency-Key': uuid4().hex}, json={'enabled': True})
        assert second.json()['version'] == 2
        listing = client.get('/api/strategies').json()['strategy_sets']
        assert listing[0]['strategy_set_id'] == 'my-project-probes'
        assert [item['version'] for item in listing[0]['versions']] == [2, 1]

        goal = helpers.post(client, '/api/career-goals/',
                            {'message': '准备Java后端面试，无简历也可以。',
                             'conditions': {'direction': 'Java后端'}}).json()
        session = helpers.post(client, f"/api/career-goals/{goal['id']}/interviews",
                               {'expected_version': 1, 'role': 'java-backend', 'max_questions': 2})
        assert session.status_code == 202, session.text
        payload = query(pg_settings[1], 'SELECT input_payload FROM app.career_interviews WHERE id=%s',
                        (session.json()['id'],))[0]['input_payload']
        cards = payload['strategies']
        assert cards[0]['id'] == 'my-lock-probe' and cards[0]['origin'] == 'user_install'
        assert cards[0]['set_version'] == 2
        assert {card['id'] for card in cards[1:]} == {'lock-verification', 'tx-tradeoff', 'idempotency-failure'}

        # Disabling affects only future sessions.
        client.patch('/api/strategies/my-project-probes', json={'enabled': False})
        assert client.delete('/api/strategies/my-project-probes').status_code == 200
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))
