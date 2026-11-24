"""F07Z-62: explicit desktop live mode.

The desktop may only go live when the user turns it on and the API has created a capped
authorization. These tests pin the contract: the cap, the policy digest, and the fact that
saving never activates by itself.
"""
import asyncio
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from interview_agent.api.app import create_app
from interview_agent.billing.config import default_policy
from interview_agent.interviews.billing import InterviewPolicy
from test_postgres import pg_settings, query, signup  # noqa: F401  (fixtures re-exported)

PASSWORD = 'Synthetic-live-9f3a2b7c1d'


def client_for_model(settings, path):
    return TestClient(create_app(settings, model_config_path=path),
                      base_url=settings.origin, raise_server_exceptions=False,
                      backend_options={'loop_factory': asyncio.SelectorEventLoop})


def test_embedded_default_policy_matches_the_config_file(project_root):
    """The frozen desktop has no configs/ directory; the embedded copy must not drift."""
    assert default_policy().digest == InterviewPolicy.load(
        project_root / 'configs/ja05-deepseek-flash.json').digest


def test_enabling_live_creates_a_capped_authorization(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    authorization = None
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            user = signup(client)['user']['id']
            response = client.put('/api/runtime/model-settings',
                                  json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                        'model': 'deepseek-chat', 'api_key': 'sk-test-123456789',
                                        'enabled': True, 'live_cap_cny': 0.5})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['enabled'] is True
            # Saving never turns the running API live; only a restart does.
            assert body['active'] is False
            stored = json.loads(model.read_text(encoding='utf-8'))
            authorization = stored['live_authorization']
            assert authorization.startswith('desktop-live-')
            assert stored['live_cap_nano'] == 500_000_000
            row = query(pg_settings[1],
                        'SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s',
                        (authorization,))[0]
            expected = default_policy().with_model_settings(
                {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'})
            assert row['policy_hash'] == expected.digest
            assert row['max_nano'] == 500_000_000

            too_much = client.put('/api/runtime/model-settings',
                                   json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                         'model': 'deepseek-chat', 'enabled': True, 'live_cap_cny': 3})
            assert too_much.status_code == 400, too_much.text

            off = client.put('/api/runtime/model-settings',
                             json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                   'model': 'deepseek-chat', 'enabled': False, 'live_cap_cny': 0.5})
            assert off.status_code == 200, off.text
            assert json.loads(model.read_text(encoding='utf-8'))['enabled'] is False
    finally:
        if authorization:
            query(pg_settings[1], 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',
                  (authorization,))


def test_key_rotation_and_removal(pg_settings, tmp_path, monkeypatch):
    """Replacing a key works; removing it must also switch live off and forget the grant."""
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    with client_for_model(pg_settings[0], model) as client:
        client.headers['Origin'] = pg_settings[0].origin
        signup(client)
        first = client.put('/api/runtime/model-settings',
                           json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                 'model': 'deepseek-chat', 'api_key': 'sk-first-123456789',
                                 'enabled': True, 'live_cap_cny': 0.2})
        assert first.status_code == 200, first.text
        mask_one = client.get('/api/runtime/model-settings').json()['key_masked']
        second = client.put('/api/runtime/model-settings',
                            json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                  'model': 'deepseek-chat', 'api_key': 'sk-second-987654321',
                                  'enabled': True, 'live_cap_cny': 0.2})
        assert second.status_code == 200, second.text
        mask_two = client.get('/api/runtime/model-settings').json()['key_masked']
        assert mask_two and mask_two != mask_one

        removed = client.delete('/api/runtime/model-settings')
        assert removed.status_code == 200, removed.text
        assert removed.json()['deleted'] is True and removed.json()['enabled'] is False
        state = client.get('/api/runtime/model-settings').json()
        assert state['key_masked'] == '' and state['enabled'] is False
        assert state['live_authorization'] == ''
        stored = json.loads(model.read_text(encoding='utf-8'))
        assert stored['api_key'] == '' and stored['enabled'] is False


def test_live_cap_is_bounded_by_the_database_contract(pg_settings, tmp_path, monkeypatch):
    """Even if the API were bypassed, the table refuses anything above 2 CNY."""
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    with client_for_model(pg_settings[0], tmp_path / 'model.json') as client:
        client.headers['Origin'] = pg_settings[0].origin
        signup(client)
        response = client.put('/api/runtime/model-settings',
                              json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                    'model': 'deepseek-chat', 'api_key': 'sk-test-123456789',
                                    'enabled': True, 'live_cap_cny': 2.5})
        assert response.status_code == 400


def test_the_planning_switch_creates_its_own_capped_authorization(pg_settings, tmp_path, monkeypatch):
    """S03c3: planning is a third capability. It must never ride on the interview flag, and its
    authorization must be its own row with its own cap."""
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    created = []
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            body = client.put('/api/runtime/model-settings',
                              json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                    'model': 'deepseek-chat', 'api_key': 'sk-test-123456789',
                                    'prep_enabled': True, 'prep_live_cap_cny': 0.2}).json()
            created.append(body['prep_live_authorization'])
            assert body['prep_enabled'] is True
            assert body['prep_live_authorization'].startswith('desktop-prep-live-')
            stored = json.loads(model.read_text(encoding='utf-8'))
            assert stored['prep_enabled'] is True and stored['prep_live_cap_nano'] == 200_000_000
            # Turning the planning switch on must not create or touch the interview one.
            assert not stored.get('live_authorization')
            row = query(pg_settings[1],
                        'SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s',
                        (body['prep_live_authorization'],))[0]
            expected = default_policy().with_model_settings(
                {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'})
            assert row['policy_hash'] == expected.digest and row['max_nano'] == 200_000_000

            shown = client.get('/api/runtime/model-settings').json()
            assert shown['prep_enabled'] is True
            assert shown['prep_live_authorization'] == body['prep_live_authorization']
            assert shown['prep_live_cap_cny'] == 0.2

            off = client.put('/api/runtime/model-settings',
                             json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                   'model': 'deepseek-chat', 'prep_enabled': False}).json()
            assert off['prep_enabled'] is False
            assert json.loads(model.read_text(encoding='utf-8'))['prep_enabled'] is False
    finally:
        for identity in created:
            if identity:
                query(pg_settings[1], 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',
                      (identity,))


def test_removing_the_key_also_switches_planning_off(pg_settings, tmp_path, monkeypatch):
    """S03c3b: the planning switch was added after `remove_key` was written, so it used to stay
    armed: the desktop host would still start live planning and every preparation failed on
    missing credentials instead of falling back to the free plan."""
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    created = []
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            saved = client.put('/api/runtime/model-settings',
                               json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                     'model': 'deepseek-chat', 'api_key': 'sk-planning-123456789',
                                     'prep_enabled': True, 'prep_live_cap_cny': 0.2})
            assert saved.status_code == 200, saved.text
            authorization = saved.json()['prep_live_authorization']
            assert authorization.startswith('desktop-prep-live-')
            created.append(authorization)

            removed = client.delete('/api/runtime/model-settings')
            assert removed.status_code == 200, removed.text
            state = client.get('/api/runtime/model-settings').json()
            stored = json.loads(model.read_text(encoding='utf-8'))
            assert state['prep_enabled'] is False
            assert state['prep_live_authorization'] == ''
            assert stored['prep_enabled'] is False
            assert stored['prep_live_authorization'] == ''
            assert stored['prep_live_cap_nano'] == ''
    finally:
        for identity in created:
            if identity:
                query(pg_settings[1], 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',
                      (identity,))
