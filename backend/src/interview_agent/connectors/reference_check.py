"""Read-only check that a topic's official reference URL still resolves.

This is deliberately narrow: the URL must be one of the product's own reference links (the hosts
below), no query text is added, and nothing from the resume, answers or private banks is sent.
It is a user-triggered utility, never a model tool, and it is off unless the user enables it.
"""
import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

ALLOWED_HOSTS = ('redis.io', 'postgresql.org', 'rfc-editor.org', 'docs.oracle.com', 'json-schema.org')
CONNECTOR_ID = 'reference-check'
MAX_BYTES = 64 * 1024
TITLE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != 'https':
        return False
    if parts.username or parts.password or parts.port not in (None, 443):
        return False
    host = host_of(url)
    return any(host == item or host.endswith('.' + item) for item in ALLOWED_HOSTS)


def catalog() -> list[dict]:
    """The official references the product already ships, per topic."""
    from interview_agent.interviews.catalog import TOPICS
    items = []
    for topic_id, topic in TOPICS.items():
        url = str(topic.get('url') or '')
        if url:
            items.append({'topic_id': topic_id, 'title': topic.get('title') or topic_id, 'url': url})
    return items


def _title(text: str) -> str:
    match = TITLE.search(text[:MAX_BYTES])
    if not match:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(match.group(1))).strip()[:120]


async def check_reference(url: str, *, transport=None, timeout: float = 5.0) -> dict:
    """One GET (plus at most one same-allowlist redirect). Failures are recorded, not raised."""
    checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if not allowed(url):
        return {'url': url, 'status': 'blocked', 'reason': 'url_not_allowed',
                'http_status': None, 'final_url': None, 'title': '', 'checked_at': checked_at}
    target = url
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False,
                                     trust_env=False,
                                     headers={'User-Agent': 'interview-agent-reference-check/1'}) as client:
            for attempt in range(2):
                response = await client.get(target)
                if response.status_code in (301, 302, 303, 307, 308) and attempt == 0:
                    location = response.headers.get('Location')
                    if not location:
                        break
                    target = urljoin(target, location)
                    if not allowed(target):
                        return {'url': url, 'status': 'blocked', 'reason': 'redirect_not_allowed',
                                'http_status': response.status_code, 'final_url': target,
                                'title': '', 'checked_at': checked_at}
                    continue
                break
            status = response.status_code
            if 200 <= status < 300:
                title = _title(response.text)
                return {'url': url, 'status': 'ok', 'reason': None, 'http_status': status,
                        'final_url': target, 'title': title, 'checked_at': checked_at}
            return {'url': url, 'status': 'not_found' if status in (404, 410) else 'error',
                    'reason': f'http_{status}', 'http_status': status, 'final_url': target,
                    'title': '', 'checked_at': checked_at}
    except (httpx.TimeoutException, httpx.TransportError):
        return {'url': url, 'status': 'unreachable', 'reason': 'network_error',
                'http_status': None, 'final_url': target, 'title': '', 'checked_at': checked_at}
