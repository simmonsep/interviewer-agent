"""Bounded preparation lookup over an explicit catalog of official documentation."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re

import httpx

CONNECTOR_ID = 'preparation-reference'
MAX_BYTES = 128 * 1024


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    locator: str


CATALOG = {
    'langgraph-checkpoints': Source(
        'LangGraph · Persistence',
        'https://docs.langchain.com/oss/python/langgraph/persistence.md',
        "graph state as checkpoints",
    ),
    'langgraph-interrupts': Source(
        'LangGraph · Interrupts',
        'https://docs.langchain.com/oss/python/langgraph/interrupts.md',
        'node restarts from the beginning',
    ),
    'langgraph-stores': Source(
        'LangGraph · Stores',
        'https://docs.langchain.com/oss/python/langgraph/stores.md',
        'Compile the graph with both the checkpointer and the store',
    ),
    'langchain-structured-output': Source(
        'LangChain · Structured output',
        'https://docs.langchain.com/oss/python/langchain/structured-output.md',
        "For models that don't support native structured output",
    ),
}


def explicit_topic(target):
    """Map clear requests to the catalog. Leave ambiguous requests to the Planner."""
    text = target.casefold()
    if 'langgraph' in text:
        if any(term in text for term in ('interrupt', '中断', '人工确认')):
            return 'langgraph-interrupts'
        if (re.search(r'(?a)\b(?:stores?|postgresstore|inmemorystore)\b', text)
                or any(term in text for term in ('跨线程记忆', '长期记忆', 'long-term memory'))):
            return 'langgraph-stores'
        if any(term in text for term in ('checkpoint', '检查点', '持久化', '会话恢复')):
            return 'langgraph-checkpoints'
    if 'langchain' in text and any(term in text for term in (
            'structured output', '结构化输出', 'toolstrategy', 'providerstrategy')):
        return 'langchain-structured-output'
    return None


def passage(text, locator):
    if not text.lstrip().startswith(('#', '>')):
        return ''
    fence = None
    matches = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('~~~', '```')):
            marker = line[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        elif not fence and not line.startswith(('#', '>', '<', '|')):
            if locator in line and 20 <= len(line) <= 420:
                matches.append(line)
    return matches[0] if len(matches) == 1 else ''


class PreparationReference:
    """One attempt per planning run. URLs come from the host, never from tool arguments."""

    def __init__(self, enabled, *, transport=None):
        self.enabled = enabled
        self.transport = transport
        self.status = 'not_requested'
        self.source = None

    async def lookup(self, topic_id):
        source = CATALOG.get(topic_id)
        if source is None:
            return {'status': 'unknown_topic'}
        if self.status != 'not_requested':
            return {'status': 'lookup_limit'}
        if not self.enabled():
            self.status = 'disabled'
            return {'status': self.status}
        self.status = 'unavailable'
        try:
            async with asyncio.timeout(5), httpx.AsyncClient(
                    transport=self.transport, follow_redirects=False, trust_env=False,
                    headers={'Accept': 'text/markdown, text/plain',
                             'User-Agent': 'interview-agent-preparation-reference/1'}) as client:
                async with client.stream('GET', source.url) as response:
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if response.status_code != 200 or content_type not in ('text/markdown', 'text/plain'):
                        return {'status': self.status}
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_BYTES:
                            return {'status': self.status}
                        body.extend(chunk)
            text = body.decode('utf-8')
        except (TimeoutError, httpx.HTTPError, UnicodeError):
            return {'status': self.status}

        # Extract a narrow passage from Markdown prose, never an HTML, code or title-only result.
        excerpt = passage(text, source.locator)
        if not excerpt:
            self.status = 'no_passage'
            return {'status': self.status}
        self.source = {
            'id': topic_id, 'title': source.title, 'url': source.url, 'excerpt': excerpt,
            'content_sha256': sha256(body).hexdigest(),
            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
        self.status = 'retrieved'
        return {'status': self.status, 'source': self.source}

    def citation(self, source_id, quote):
        if not self.source or source_id != self.source['id'] or not isinstance(quote, str):
            return None
        if 20 <= len(quote) <= 300 and quote in self.source['excerpt']:
            return {key: value for key, value in self.source.items() if key != 'excerpt'} | {'quote': quote}
        return None
