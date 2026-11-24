"""Search provider contracts. Only titles and URLs leave this adapter."""
from dataclasses import dataclass
from typing import Literal

Provider = Literal['baidu', 'brave', 'custom']
BAIDU_URL = 'https://qianfan.baidubce.com/v2/ai_search/web_search'
BRAVE_URL = 'https://api.search.brave.com/res/v1/web/search'
ENDPOINTS = {'baidu': BAIDU_URL, 'brave': BRAVE_URL}


@dataclass(frozen=True)
class SearchService:
    provider: Provider
    endpoint: str

    def request(self, key, query, site):
        if self.provider == 'brave':
            return 'GET', {
                'headers': {'X-Subscription-Token': key},
                'params': {'q': f'{query} site:{site}', 'count': 5, 'result_filter': 'web',
                           'text_decorations': 'false', 'safesearch': 'strict'},
            }
        if self.provider == 'baidu':
            body = {'messages': [{'role': 'user', 'content': query}], 'edition': 'standard',
                    'search_source': 'baidu_search_v2', 'resource_type_filter': [{'type': 'web', 'top_k': 5}],
                    'search_filter': {'match': {'site': [site]}}, 'safe_search': True}
        else:
            body = {'query': query, 'site': site, 'limit': 5}
        return 'POST', {'headers': {'Authorization': f'Bearer {key}'}, 'json': body}

    def results(self, payload):
        if not isinstance(payload, dict) or payload.get('code') not in (None, 0, '0'):
            raise ValueError('search_provider_error')
        if self.provider == 'baidu':
            rows = payload.get('references', [])
        elif self.provider == 'brave':
            web = payload.get('web') or {}
            rows = web.get('results', []) if isinstance(web, dict) else None
        else:
            rows = payload.get('results')
        if not isinstance(rows, list):
            raise ValueError('invalid_search_results')
        return rows
