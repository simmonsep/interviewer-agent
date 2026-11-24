"""One real call, to prove the connection the user configured actually works.

This is deliberately NOT the assessment path. It answers a single question:
"with this base_url, model name and key, does the endpoint answer?" - because
until now the settings pane only wrote a file, and the workers read a policy
file with a hard-coded endpoint, so a filled-in key proved nothing.

Any OpenAI-compatible endpoint works here (DeepSeek is just the default
template): the request is the plain chat-completions shape, no tools, no
streaming, a tiny output budget.
"""
from __future__ import annotations

import time

import httpx


class ProbeError(Exception):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _classify(status: int) -> str:
    """Provider status -> a code the UI can explain without leaking a key."""
    if status == 401:
        return 'model_key_rejected'
    if status == 403:
        return 'model_key_forbidden'
    if status == 404:
        return 'model_or_path_not_found'
    if status == 429:
        return 'model_rate_limited'
    if 500 <= status < 600:
        return 'model_provider_error'
    return f'model_http_{status}'


async def probe(*, base_url: str, model: str, key: str, timeout: float = 20.0) -> dict:
    """Send one minimal completion. Raises ProbeError with a stable code."""
    if not base_url or not model or not key:
        raise ProbeError('model_settings_incomplete')
    endpoint = base_url.rstrip('/') + '/chat/completions'
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': '只回复两个字：可用'}],
        'max_tokens': 8,
        'stream': False,
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            )
    except httpx.TimeoutException:
        raise ProbeError('model_timeout') from None
    except httpx.HTTPError as error:
        raise ProbeError('model_network_error', type(error).__name__) from None
    elapsed = int((time.monotonic() - started) * 1000)

    if response.status_code != 200:
        raise ProbeError(_classify(response.status_code))
    try:
        body = response.json()
        choice = (body.get('choices') or [{}])[0]
        text = (choice.get('message') or {}).get('content') or ''
    except ValueError:
        raise ProbeError('model_invalid_payload') from None
    return {'ok': True, 'latency_ms': elapsed, 'model': model,
            'base_url': base_url, 'reply_preview': str(text)[:40]}
