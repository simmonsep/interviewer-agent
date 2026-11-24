"""Local search-provider settings, with each credential bound to its destination."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from pydantic import Field, SecretStr

from interview_agent.contracts.common import Contract
from interview_agent.desktop import key_vault
from interview_agent.desktop.agent_policy import _write
from interview_agent.storage.errors import ApiError

from .experience_providers import ENDPOINTS, Provider, SearchService

CUSTOM_ENDPOINT_ENV = 'INTERVIEW_EXPERIENCE_SEARCH_COMPAT_URL'
KEY_ENVS = {'baidu': 'INTERVIEW_BAIDU_SEARCH_API_KEY', 'brave': 'INTERVIEW_BRAVE_SEARCH_API_KEY',
            'custom': 'INTERVIEW_EXPERIENCE_SEARCH_COMPAT_KEY'}
LABELS = {'baidu': '百度千帆 · 百度搜索标准版', 'brave': 'Brave Web Search', 'custom': '自定义兼容 API'}
HTTPS_ENDPOINT = re.compile(r'https://(?:[a-zA-Z0-9-]+\.)+[a-zA-Z][a-zA-Z0-9-]*(?::443)?/[a-zA-Z0-9._~/-]*')


class SearchSettingsInput(Contract):
    provider: Provider
    endpoint: str = Field(default='', max_length=500)
    api_key: SecretStr = Field(default=SecretStr(''), max_length=400)
    clear_key: bool = False


def services():
    available = {key: SearchService(key, url) for key, url in ENDPOINTS.items()}
    endpoint = os.environ.get(CUSTOM_ENDPOINT_ENV, '').strip()
    if HTTPS_ENDPOINT.fullmatch(endpoint):
        available['custom'] = SearchService('custom', endpoint)
    return available


def settings_path(policy_path: Path):
    return policy_path.with_name('experience-search.json')


def selection(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    provider = data.get('provider')
    if isinstance(provider, str) and provider in LABELS:
        return provider, data.get('endpoint')
    # Preserve existing env-only Brave setups. New installations start on Baidu.
    if not os.environ.get(KEY_ENVS['baidu'], '').strip() and os.environ.get(KEY_ENVS['brave'], '').strip():
        return 'brave', None
    return 'baidu', None


def selected_service(path):
    provider, endpoint = selection(path)
    service = services().get(provider)
    if service and (provider != 'custom' or endpoint == service.endpoint):
        return service
    return None


def slot(service):
    destination = sha256(service.endpoint.encode('utf-8')).hexdigest()
    return f'WorkBuddy/InterviewAgent/SearchKey/{service.provider}/{destination}'


def credential(service):
    return key_vault.load(target=slot(service)) or os.environ.get(KEY_ENVS[service.provider], '').strip()


def available_option(service):
    source = 'vault' if key_vault.load(target=slot(service)) else (
        'environment' if os.environ.get(KEY_ENVS[service.provider], '').strip() else 'missing')
    return {'id': service.provider, 'label': LABELS[service.provider], 'endpoint': service.endpoint,
            'key_source': source}


def view(path):
    provider, _ = selection(path)
    return {'provider': provider, 'vault_available': key_vault.available(),
            'providers': [available_option(service) for service in services().values()]}


def save(path, value):
    service = services().get(value.provider)
    if (service is None or (value.endpoint and value.endpoint != service.endpoint)
            or (value.provider == 'custom' and value.endpoint != service.endpoint)):
        raise ApiError('experience_search_endpoint_not_allowed', 400)
    incoming = value.api_key.get_secret_value().strip()
    if (incoming and value.clear_key) or (incoming and not re.fullmatch(r'[!-~]{1,400}', incoming)):
        raise ApiError('experience_search_key_invalid', 400)
    destination = slot(service)
    if incoming and not key_vault.store(incoming, target=destination):
        raise ApiError('experience_search_key_vault_unavailable', 409)
    if value.clear_key and key_vault.load(target=destination) and not key_vault.clear(target=destination):
        raise ApiError('experience_search_key_vault_unavailable', 409)
    _write(path, {'provider': service.provider, 'endpoint': service.endpoint})
