"""F07Z-112: the desktop can enable material rewriting live, with its own policy and cap.

The frozen app has no configs/ directory and starts workers with `--credentials vault` and no
`--policy`, so these tests pin the embedded policy, the ja04 authorization, the worker's vault
path and the settings round-trip.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from interview_agent.billing.config import default_policy
from interview_agent.billing.ledger import authorize
from interview_agent.materials.billing import MaterialPolicy, default_material_policy
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.worker import run as run_material_worker
from interview_agent.materials.worker import worker_for
from interview_agent.runtime.types import AssistantResponse, ToolCall
from test_billing import reply
from test_desktop_live_settings import client_for_model
from test_material_repository import (cleanup_auth, material_context, notification, source,
                                      start, view)
from test_postgres import arun, pg_settings, query, signup
from test_worker import resources, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

TEXT = '工作经历\n2019.03 某公司\n负责缓存改造\n技能\nPython'


def test_embedded_material_policy_matches_the_config_file(project_root):
    assert default_material_policy().digest == MaterialPolicy.load(
        project_root / 'configs/ja04-deepseek-flash.json').digest
    # The two capabilities must never share an authorization row.
    assert default_material_policy().digest != default_policy().digest


def test_enabling_material_live_creates_its_own_authorization(pg_settings, tmp_path, monkeypatch):
    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    authorization = None
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            response = client.put('/api/runtime/model-settings',
                                  json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                        'model': 'deepseek-chat', 'api_key': 'sk-test-123456789',
                                        'enabled': False, 'material_enabled': True,
                                        'material_live_cap_cny': 0.5})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['material_enabled'] is True and body['material_active'] is False
            stored = json.loads(model.read_text(encoding='utf-8'))
            authorization = stored['material_live_authorization']
            assert authorization.startswith('desktop-material-live-')
            assert stored['material_live_cap_nano'] == 500_000_000
            assert stored['enabled'] is False and stored.get('live_authorization', '') == ''
            row = query(pg_settings[1],
                        'SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s',
                        (authorization,))[0]
            expected = default_material_policy().with_model_settings(
                {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'})
            assert row['policy_hash'] == expected.digest
            assert row['max_nano'] == 500_000_000

            state = client.get('/api/runtime/model-settings').json()
            assert state['material_enabled'] is True
            assert state['material_live_authorization'] == authorization
            assert state['material_live_cap_cny'] == 0.5

            # A cap below one request's reservation can never run; refuse it instead of
            # creating an authorization that fails on the first call.
            too_small = client.put('/api/runtime/model-settings',
                                   json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                         'model': 'deepseek-chat', 'material_enabled': True,
                                         'material_live_cap_cny': 0.1})
            assert too_small.status_code == 400, too_small.text

            removed = client.delete('/api/runtime/model-settings')
            assert removed.status_code == 200
            after = client.get('/api/runtime/model-settings').json()
            assert after['material_enabled'] is False
            assert after['material_live_authorization'] == ''
            assert json.loads(model.read_text(encoding='utf-8'))['material_enabled'] is False
    finally:
        if authorization:
            # F07Z-117: a real desktop run can leave model_http_calls on this exact
            # authorization (same policy digest, same cap). Authorization rows are audit
            # data, so only drop the row when no call references it.
            query(pg_settings[1],
                  'DELETE FROM app.model_authorizations WHERE id=%s AND NOT EXISTS '
                  '(SELECT 1 FROM app.model_http_calls WHERE authorization_id=%s) RETURNING id',
                  (authorization, authorization))


def test_worker_starts_live_in_test_mode_with_vault_and_no_policy(monkeypatch):
    """The three desktop blockers must stay fixed: mode guard, policy None, credentials vault."""
    monkeypatch.setattr('interview_agent.desktop.key_vault.load', lambda: 'offline-not-real')
    config = Path(os.environ['PY_INTERVIEW_WORKER_TEST_CONFIG'])
    args = SimpleNamespace(config=config, provider='live', policy=None,
                           authorization='unused-' + uuid4().hex, credentials='vault',
                           once=True, model_config=None)
    # No queued job: this only exercises the startup path, and must not raise the old
    # `live_requires_development_and_credentials` or `Path(None)` TypeError.
    arun(run_material_worker(args))


def test_material_rewrite_runs_live_from_the_vault_with_mock_transport(material_context,
                                                                      worker_settings,
                                                                      pg_settings,
                                                                      project_root,
                                                                      monkeypatch):
    c, g = material_context
    source(c, g, text=TEXT)
    start(c, g, 1, mode='draft', draft_style='rewrite')
    policy = default_material_policy()
    auth = 'test-' + uuid4().hex
    arun(authorize(pg_settings[1], policy, auth))
    monkeypatch.setattr('interview_agent.desktop.key_vault.load', lambda: 'offline-not-real')
    seen = []
    state = {'source_id': ''}

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            name, arguments = 'read_material_scope', {}
        elif len(seen) == 2:
            observation = json.loads(payload['messages'][-1]['content'])
            state['source_id'] = observation['sources'][0]['id']
            name, arguments = 'read_target', {}
        elif len(seen) == 3:
            name, arguments = 'read_source', {'source_id': state['source_id']}
        else:
            observation = json.loads(payload['messages'][-1]['content'])
            source_id = observation['id']
            ids = {segment['text']: segment['id'] for segment in observation['segments']}
            name = 'submit_rewrite'
            arguments = {
                'items': [{'text': '负责缓存改造', 'source_ids': [source_id],
                           'change_note': '整理', 'evidence_segments': [ids['负责缓存改造']]}],
                'education': [], 'experience': [ids['2019.03 某公司']],
                'projects': [], 'skills': [ids['Python']],
                'omitted_segments': [], 'suggestions': []}
        return httpx.Response(200, json=reply(AssistantResponse(
            tool_calls=[ToolCall(id=uuid4().hex, name=name, arguments=arguments)])))

    async def process():
        async with resources(worker_settings) as (old, queue):
            from interview_agent.worker.queue import OutboxDispatcher
            repo = MaterialRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            worker = worker_for(repo, queue, provider='live', policy=None, authorization=auth,
                                credentials='vault',
                                inner_factory=lambda: httpx.MockTransport(respond))
            assert await worker.once()

    try:
        arun(process())
        run = view(c, g)['runs'][0]
        assert run['status'] == 'SUCCEEDED', run.get('error_code')
        assert run['origin'] == 'real_model'
        calls = query(pg_settings[1],
                      'SELECT state FROM app.model_http_calls WHERE authorization_id=%s', (auth,))
        assert calls and all(row['state'] == 'KNOWN' for row in calls)
    finally:
        cleanup_auth(pg_settings[1], auth)


def test_settings_reports_material_spend_without_the_key(pg_settings, tmp_path, monkeypatch):
    """F07Z-120: a reused authorization is only honest if the pane shows what is spent.

    The document asks the user to raise the total when the remaining money cannot cover one
    reservation; that advice is useless without the number. The response must never carry the
    key itself (only the mask).
    """
    from psycopg.types.json import Jsonb

    monkeypatch.setenv('INTERVIEW_KEY_VAULT', 'off')
    model = tmp_path / 'model.json'
    authorization = None
    call_id = uuid4()
    secret = 'sk-test-123456789'
    try:
        with client_for_model(pg_settings[0], model) as client:
            client.headers['Origin'] = pg_settings[0].origin
            signup(client)
            # A distinct model name keeps the derived authorization id away from the row a real
            # desktop run leaves behind in this same database.
            response = client.put('/api/runtime/model-settings',
                                  json={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                        'model': 'deepseek-chat-spend-test', 'api_key': secret,
                                        'enabled': False, 'material_enabled': True,
                                        'material_live_cap_cny': 0.5})
            assert response.status_code == 200, response.text
            authorization = response.json()['material_live_authorization']
            assert authorization.startswith('desktop-material-live-')
            state = client.get('/api/runtime/model-settings').json()
            assert state['material_spent_cny'] == 0.0
            assert state['material_remaining_cny'] == 0.5

            query(pg_settings[1],
                  'INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,'
                  'attempt,fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,'
                  'state,accounted_nano,raw_usage) VALUES(%s,%s,%s,%s,1,1,%s,%s,100,%s,'
                  "'KNOWN',%s,%s) RETURNING id",
                  (call_id, authorization, uuid4(), uuid4(), 'a' * 64, 'b' * 64, 50_000_000,
                   20_000_000, Jsonb({'prompt_tokens': 10})))
            state = client.get('/api/runtime/model-settings').json()
            assert round(state['material_spent_cny'], 8) == 0.02
            assert round(state['material_remaining_cny'], 8) == 0.48
            assert secret not in json.dumps(state)
    finally:
        query(pg_settings[1], 'DELETE FROM app.model_http_calls WHERE id=%s RETURNING id', (call_id,))
        if authorization:
            query(pg_settings[1],
                  'DELETE FROM app.model_authorizations WHERE id=%s AND NOT EXISTS '
                  '(SELECT 1 FROM app.model_http_calls WHERE authorization_id=%s) RETURNING id',
                  (authorization, authorization))
