"""F07Z-165 (arbitration THIRD_OPTION): one explicit enable is one authorization instance.

The desk settings endpoint used to derive the authorization row id from `policy digest + cap`,
so the same model and cap always reused one row: its lifetime `max_nano` carried older spend,
an UNKNOWN call paused it, and the settings pane had no way to start a fresh budget. These tests
pin the replacement: an explicit new authorization gets its own row, a retry of the same request
is idempotent, a plain save only re-binds the current row, and legacy callers keep the old id.
"""
import asyncio
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from interview_agent.api.app import create_app
from interview_agent.billing.config import default_policy
from test_postgres import pg_settings, query, signup  # noqa: F401  (fixtures re-exported)


def client_for_model(settings, path):
    return TestClient(create_app(settings, model_config_path=path),
                      base_url=settings.origin, raise_server_exceptions=False,
                      backend_options={'loop_factory': asyncio.SelectorEventLoop})


def payload(**overrides):
    body = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
            'model': 'deepseek-chat', 'api_key': 'sk-instance-123456789',
            'enabled': True, 'live_cap_cny': 0.5}
    body.update(overrides)
    return body


def cleanup(owner, identities):
    for identity in identities:
        if not identity:
            continue
        query(owner, 'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',
              (identity,))
        query(owner, 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id', (identity,))


def stored(model):
    return json.loads(model.read_text(encoding='utf-8'))


def test_each_explicit_enable_gets_its_own_row_and_cap(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    created = []
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            first_request, second_request = str(uuid4()), str(uuid4())
            first = client.put('/api/runtime/model-settings',
                               json=payload(new_authorization=True,
                                            authorization_request_id=first_request))
            assert first.status_code == 200, first.text
            one = first.json()['live_authorization']
            created.append(one)
            second = client.put('/api/runtime/model-settings',
                                json=payload(new_authorization=True,
                                             authorization_request_id=second_request,
                                             live_cap_cny=0.9))
            assert second.status_code == 200, second.text
            two = second.json()['live_authorization']
            created.append(two)

            assert one == f'desktop-live-{first_request}'
            assert two == f'desktop-live-{second_request}'
            rows = query(pg_settings[1],
                         'SELECT id,max_nano,state FROM app.model_authorizations '
                         "WHERE id IN (%s,%s) ORDER BY max_nano", (one, two))
            assert [(row['max_nano'], row['state']) for row in rows] == [
                (500_000_000, 'ACTIVE'), (900_000_000, 'ACTIVE')]
            assert stored(model)['live_authorization'] == two
    finally:
        cleanup(pg_settings[1], created)


def test_retrying_the_same_request_is_idempotent(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    identity = None
    request_id = str(uuid4())
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            for _ in range(2):
                saved = client.put('/api/runtime/model-settings',
                                   json=payload(new_authorization=True,
                                                authorization_request_id=request_id))
                assert saved.status_code == 200, saved.text
                assert identity in (None, saved.json()['live_authorization'])
                identity = saved.json()['live_authorization']
            rows = query(pg_settings[1],
                         'SELECT count(*) AS n FROM app.model_authorizations WHERE id=%s',
                         (identity,))
            assert rows[0]['n'] == 1
            # The retry must not rewrite the cap or the spend history of the row.
            row = query(pg_settings[1],
                        'SELECT max_nano FROM app.model_authorizations WHERE id=%s',
                        (identity,))[0]
            assert row['max_nano'] == 500_000_000
    finally:
        cleanup(pg_settings[1], [identity])


def test_a_paused_row_does_not_block_a_new_authorization(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    created = []
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            first = client.put('/api/runtime/model-settings',
                               json=payload(new_authorization=True,
                                            authorization_request_id=str(uuid4())))
            paused = first.json()['live_authorization']
            created.append(paused)
            query(pg_settings[1],
                  "UPDATE app.model_authorizations SET state='PAUSED' WHERE id=%s RETURNING id",
                  (paused,))
            shown = client.get('/api/runtime/model-settings').json()
            assert shown['live_state'] == 'PAUSED'
            assert shown['live_remaining_cny'] is not None

            fresh = client.put('/api/runtime/model-settings',
                               json=payload(new_authorization=True,
                                            authorization_request_id=str(uuid4())))
            assert fresh.status_code == 200, fresh.text
            replacement = fresh.json()['live_authorization']
            created.append(replacement)
            assert replacement != paused
            row = query(pg_settings[1],
                        'SELECT state FROM app.model_authorizations WHERE id=%s',
                        (replacement,))[0]
            assert row['state'] == 'ACTIVE'
            assert query(pg_settings[1],
                         'SELECT state FROM app.model_authorizations WHERE id=%s',
                         (paused,))[0]['state'] == 'PAUSED'
    finally:
        cleanup(pg_settings[1], created)


def test_a_plain_save_rebinds_without_minting_budget(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    identity = None
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            first = client.put('/api/runtime/model-settings',
                               json=payload(new_authorization=True,
                                            authorization_request_id=str(uuid4())))
            identity = first.json()['live_authorization']
            before = query(pg_settings[1],
                           'SELECT count(*) AS n FROM app.model_authorizations '
                           'WHERE id LIKE %s', ('desktop-live-%',))[0]['n']

            plain = client.put('/api/runtime/model-settings',
                               json=payload(api_key='sk-rotated-987654321',
                                            new_authorization=False,
                                            authorization_request_id=''))
            assert plain.status_code == 200, plain.text
            assert plain.json()['live_authorization'] == identity
            assert stored(model)['live_authorization'] == identity
            after = query(pg_settings[1],
                          'SELECT count(*) AS n FROM app.model_authorizations '
                          'WHERE id LIKE %s', ('desktop-live-%',))[0]['n']
            assert after == before

            # A changed cap is a new intent; without an explicit new request the server says so
            # instead of silently reusing (or silently creating) a row.
            changed = client.put('/api/runtime/model-settings',
                                 json=payload(new_authorization=False,
                                              authorization_request_id='', live_cap_cny=0.9))
            assert changed.status_code == 409, changed.text
            assert changed.json()['code'] == 'authorization_requires_new_instance'
            assert stored(model)['live_authorization'] == identity
    finally:
        cleanup(pg_settings[1], [identity])


def test_legacy_callers_keep_the_deterministic_id(pg_settings, tmp_path, monkeypatch):
    """An older client sends neither new field; its model.json and behaviour must not change."""
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    created = []
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            policy = default_policy().with_model_settings(
                {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'})
            legacy = f'desktop-live-{policy.digest[:10]}-500'
            created.append(legacy)
            saved = client.put('/api/runtime/model-settings', json=payload())
            assert saved.status_code == 200, saved.text
            assert saved.json()['live_authorization'] == legacy
            again = client.put('/api/runtime/model-settings', json=payload())
            assert again.json()['live_authorization'] == legacy
            assert query(pg_settings[1],
                         'SELECT max_nano FROM app.model_authorizations WHERE id=%s',
                         (legacy,))[0]['max_nano'] == 500_000_000
    finally:
        cleanup(pg_settings[1], created)


def test_get_reports_state_spend_and_remaining(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    identity = None
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            saved = client.put('/api/runtime/model-settings',
                               json=payload(new_authorization=True,
                                            authorization_request_id=str(uuid4())))
            identity = saved.json()['live_authorization']
            shown = client.get('/api/runtime/model-settings').json()
            assert shown['live_state'] == 'ACTIVE'
            assert shown['live_spent_cny'] == 0.0
            assert shown['live_remaining_cny'] == 0.5
            # Nothing is bound for the other capabilities yet.
            assert shown['material_state'] == '' and shown['material_spent_cny'] is None
            assert shown['prep_state'] == '' and shown['prep_spent_cny'] is None

            query(pg_settings[1],
                  'INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,'
                  'attempt,fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,'
                  'state,accounted_nano,raw_usage) '
                  "VALUES(%s,%s,%s,%s,1,1,%s,%s,100,100000000,'KNOWN',25000000,'{}'::jsonb) "
                  'RETURNING id',
                  (uuid4(), identity, uuid4(), uuid4(), 'a' * 64, 'b' * 64))
            shown = client.get('/api/runtime/model-settings').json()
            assert shown['live_spent_cny'] == 0.025
            assert shown['live_remaining_cny'] == 0.475
    finally:
        cleanup(pg_settings[1], [identity])


def test_an_invalid_request_id_is_refused_without_a_row(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            response = client.put('/api/runtime/model-settings',
                                  json=payload(new_authorization=True,
                                               authorization_request_id='../escape'))
            assert response.status_code == 400, response.text
            assert response.json()['code'] == 'authorization_request_id_invalid'
            assert not model.exists() or 'live_authorization' not in stored(model)
    finally:
        cleanup(pg_settings[1], [])
