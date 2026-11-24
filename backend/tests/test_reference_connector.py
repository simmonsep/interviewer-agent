"""F07Z-162: allowlisted read-only reference check (off by default, never a model tool)."""
import json

import httpx
import pytest

from interview_agent.connectors import reference_check
from interview_agent.connectors.routes import CONNECTOR_INFO

from test_postgres import pg_settings  # noqa: F401  (pytest fixture reuse)


def test_only_the_products_own_official_https_urls_are_allowed():
    assert reference_check.allowed('https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/')
    assert reference_check.allowed('https://www.postgresql.org/docs/18/transaction-iso.html')
    assert reference_check.allowed('https://docs.oracle.com/javase/specs/jls/se25/html/jls-17.html')
    assert not reference_check.allowed('http://redis.io/docs/')
    assert not reference_check.allowed('https://example.com/docs')
    assert not reference_check.allowed('https://user:pass@redis.io/docs/')
    assert not reference_check.allowed('https://redis.io:8443/docs/')


def test_the_catalog_lists_the_five_authored_references():
    items = reference_check.catalog()
    assert len(items) == 5
    assert {item['topic_id'] for item in items} == {
        'redis-lock', 'sql-isolation', 'http-idempotency', 'java-concurrency', 'agent-tools'}
    assert all(reference_check.allowed(item['url']) for item in items)


def transport(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(500))
    return httpx.MockTransport(handler)


def test_a_reachable_page_reports_status_title_and_final_url():
    import asyncio

    url = 'https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/'
    result = asyncio.run(reference_check.check_reference(url, transport=transport({
        url: httpx.Response(200, text='<html><title>Distributed locks\n | Redis</title></html>')})))
    assert result['status'] == 'ok' and result['http_status'] == 200
    assert result['title'] == 'Distributed locks | Redis'


def test_missing_and_unreachable_are_recorded_not_raised():
    import asyncio

    url = 'https://www.postgresql.org/docs/18/transaction-iso.html'
    missing = asyncio.run(reference_check.check_reference(url, transport=transport({url: httpx.Response(404)})))
    assert missing['status'] == 'not_found' and missing['http_status'] == 404

    def boom(request):
        raise httpx.ConnectTimeout('timeout', request=request)
    timed_out = asyncio.run(reference_check.check_reference(url, transport=httpx.MockTransport(boom)))
    assert timed_out['status'] == 'unreachable' and timed_out['reason'] == 'network_error'


def test_a_redirect_outside_the_allowlist_is_blocked():
    import asyncio

    url = 'https://redis.io/docs/latest/'
    result = asyncio.run(reference_check.check_reference(url, transport=transport({
        url: httpx.Response(301, headers={'Location': 'https://example.com/elsewhere'})})))
    assert result['status'] == 'blocked' and result['reason'] == 'redirect_not_allowed'


def test_a_redirect_inside_the_allowlist_is_followed_once():
    import asyncio

    start = 'https://postgresql.org/docs/18/transaction-iso.html'
    final = 'https://www.postgresql.org/docs/18/transaction-iso.html'
    result = asyncio.run(reference_check.check_reference(start, transport=transport({
        start: httpx.Response(301, headers={'Location': final}),
        final: httpx.Response(200, text='<title>Transaction Isolation</title>')})))
    assert result['status'] == 'ok' and result['final_url'] == final
    assert result['title'] == 'Transaction Isolation'


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / 'agent-policy.json'
    monkeypatch.setenv('INTERVIEW_AGENT_POLICY_PATH', str(path))
    return path


@pytest.mark.postgres
def test_the_connector_is_off_until_enabled_and_gates_the_request(pg_settings, policy_file, monkeypatch):
    from test_postgres import client_for, signup

    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        signup(client)
        listing = client.get('/api/runtime/connectors').json()
        connector = listing['connectors'][0]
        assert connector['id'] == 'reference-check' and connector['enabled'] is False
        assert len(connector['items']) == 5
        automatic = listing['connectors'][1]
        assert automatic['id'] == 'preparation-reference' and automatic['enabled'] is False
        disabled = client.post('/api/runtime/connectors/reference-check',
                               json={'url': connector['items'][0]['url']})
        assert disabled.status_code == 409 and disabled.json()['code'] == 'connector_disabled'

        enabled = client.put('/api/runtime/connectors/reference-check', json={'enabled': True})
        assert enabled.status_code == 200 and enabled.json()['connectors'][0]['enabled'] is True
        assert json.loads(policy_file.read_text(encoding='utf-8'))['connectors'] == {'reference-check': True}
        both = client.put('/api/runtime/connectors/preparation-reference', json={'enabled': True}).json()
        assert [item['enabled'] for item in both['connectors']] == [True, True, False]
        experiences = client.put('/api/runtime/connectors/preparation-interview-experiences',
                                 json={'enabled': True})
        assert experiences.status_code == 200

        # Changing the autonomy mode must not drop the connector choice.
        client.put('/api/runtime/agent-policy', json={'mode': 'approve'})
        assert [item['enabled'] for item in client.get('/api/runtime/connectors').json()['connectors']] == [True, True, True]

        refused = client.post('/api/runtime/connectors/reference-check',
                              json={'url': 'https://example.com/docs'})
        assert refused.status_code == 400 and refused.json()['code'] == 'reference_url_not_allowed'

        import asyncio

        async def fake(url, **_):
            return {'url': url, 'status': 'ok', 'reason': None, 'http_status': 200,
                    'final_url': url, 'title': 'Synthetic', 'checked_at': '2026-11-19T00:00:00+00:00'}
        monkeypatch.setattr(reference_check, 'check_reference', fake)
        checked = client.post('/api/runtime/connectors/reference-check',
                              json={'url': connector['items'][0]['url']})
        assert checked.status_code == 200 and checked.json()['status'] == 'ok'
        assert asyncio.run(reference_check.check_reference('x'))['status'] == 'ok'


def test_the_interviewer_still_has_no_connector_tool():
    from interview_agent.interviews.agent import tools

    names = [tool.name for tool in tools()]
    assert names == ['read_interview_scope', 'read_topics', 'read_answer', 'submit_interview_action']
    assert 'reference-check' in CONNECTOR_INFO


@pytest.mark.postgres
def test_experience_settings_only_use_provider_bound_vault_secrets(pg_settings, policy_file, monkeypatch):
    from test_postgres import client_for, signup
    from interview_agent.connectors import experience_settings as settings
    from interview_agent.connectors.experience_providers import BAIDU_URL, BRAVE_URL

    values = {}

    def store(key, *, target):
        values[target] = key
        return True

    monkeypatch.setattr(settings.key_vault, 'store', store)
    monkeypatch.setattr(settings.key_vault, 'load', lambda *, target: values.get(target))
    monkeypatch.setattr(settings.key_vault, 'clear', lambda *, target: bool(values.pop(target, None)))
    monkeypatch.setattr(settings.key_vault, 'available', lambda: True)
    for name in (*settings.KEY_ENVS.values(), settings.CUSTOM_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    path = settings.settings_path(policy_file)
    endpoint = '/api/runtime/connectors/experience-search/settings'

    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        assert client.put(endpoint, json={'provider': 'baidu'}).status_code == 401
        signup(client)

        def search_config(response):
            assert response.status_code == 200, response.text
            return response.json()['connectors'][2]

        initial = search_config(client.get('/api/runtime/connectors'))
        assert initial['search_settings']['provider'] == 'baidu'
        assert not initial['enabled']
        denied = client.put(endpoint, json={'provider': 'custom', 'endpoint': 'https://unsafe.example.com/query',
                                           'api_key': 'test-invalid-target'})
        assert denied.status_code == 400 and not values
        baidu = client.put(endpoint, json={'provider': 'baidu', 'endpoint': BAIDU_URL, 'api_key': 'baidu-secret-demo'})
        assert search_config(baidu)['search_settings']['providers'][0]['key_source'] == 'vault'
        assert 'baidu-secret-demo' not in baidu.text and 'baidu-secret-demo' not in path.read_text()
        assert settings.credential(settings.selected_service(path)) == 'baidu-secret-demo'

        brave = client.put(endpoint, json={'provider': 'brave', 'endpoint': BRAVE_URL, 'api_key': 'brave-secret-demo'})
        after = search_config(brave)
        assert after['search_settings']['provider'] == 'brave' and 'api.search.brave.com' in after['domains']
        assert settings.credential(settings.selected_service(path)) == 'brave-secret-demo'
        assert 'brave-secret-demo' not in brave.text and 'brave-secret-demo' not in path.read_text()
        assert search_config(client.put(endpoint, json={'provider': 'baidu'}))['search_settings']['provider'] == 'baidu'
        assert settings.credential(settings.selected_service(path)) == 'baidu-secret-demo'

        custom = 'https://search.example.com/query'
        monkeypatch.setenv(settings.CUSTOM_ENDPOINT_ENV, custom)
        result = client.put(endpoint, json={'provider': 'custom', 'endpoint': custom, 'api_key': 'custom-secret-demo'})
        assert search_config(result)['search_settings']['provider'] == 'custom'
        assert settings.credential(settings.selected_service(path)) == 'custom-secret-demo'
        assert 'custom-secret-demo' not in result.text
        monkeypatch.setenv(settings.CUSTOM_ENDPOINT_ENV, 'https://another.example.com/query')
        assert settings.selected_service(path) is None
        assert not settings.credential(settings.services()['custom'])

        cleared = client.put(endpoint, json={'provider': 'baidu', 'clear_key': True})
        assert search_config(cleared)['search_settings']['providers'][0]['key_source'] == 'missing'
        assert settings.credential(settings.services()['brave']) == 'brave-secret-demo'


def test_existing_brave_environment_config_keeps_working(tmp_path, monkeypatch):
    from interview_agent.connectors import experience_settings as settings
    monkeypatch.delenv(settings.KEY_ENVS['baidu'], raising=False)
    monkeypatch.setenv(settings.KEY_ENVS['brave'], 'legacy-key')
    assert settings.selected_service(tmp_path / 'experience-search.json').provider == 'brave'
