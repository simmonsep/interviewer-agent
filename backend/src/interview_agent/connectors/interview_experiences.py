"""Read bounded public interview question clues during full preparation."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

import httpx

from interview_agent.career.content import review_flags, visible_text
from .experience_providers import BRAVE_URL, SearchService

CONNECTOR_ID = 'preparation-interview-experiences'
SEARCH_URL = BRAVE_URL
POST_HOST = 'www.nowcoder.com'
POST_PATH = re.compile(r'/(?:discuss/[0-9]{1,24}|feed/main/detail/[0-9a-f]{32})/?')
MAX_SEARCH_BYTES = 64 * 1024
MAX_PAGE_BYTES = 512 * 1024
PAGE_LIMIT = 2
PASSAGE_CHARS = 420
PASSAGES_PER_PAGE = 2
POST_BODY_CLASSES = ('nc-slate-editor-content', 'post-content-box')
PAGE_CHROME = ('head', 'title', 'nav', 'header', 'footer', 'aside')
ROLE_TERMS = {'ai-application': 'AI Agent', 'java-backend': 'Java 后端'}
LEVELS = ('实习', '校招', '社招')
TECH_TERMS = {
    'LangGraph': ('LangGraph',), 'LangChain': ('LangChain',), 'Python': ('Python',),
    '检索增强': ('RAG', '检索增强', '向量检索'), '重排': ('Rerank', '重排', '重排序'),
    '工具调用': ('工具调用', '工具执行', 'Function Calling'), 'MCP': ('MCP',),
    '评测': ('评测', 'benchmark', '故障归因'), '会话状态': ('状态管理', '会话恢复', '检查点'),
    '上下文压缩': ('上下文压缩', '记忆压缩'), '提示词': ('提示词', 'Prompt'),
    'Redis': ('Redis',), 'MySQL': ('MySQL',), 'PostgreSQL': ('PostgreSQL',),
    'Spring Boot': ('Spring Boot', 'SpringBoot'), '消息队列': ('消息队列', 'Kafka', 'RabbitMQ'),
    '并发控制': ('并发控制', '分布式锁', '死锁'), '幂等': ('幂等', 'idempotency'),
    '事务': ('事务', 'MVCC'), 'JVM': ('JVM',), '微服务': ('微服务',),
}
QUESTION = re.compile(r'[？?]|如何|怎么|为什么|区别|问了|追问', re.IGNORECASE)
INTERVIEW_ROUND = re.compile(
    r'(?:^|[\s｜|·/【】《》()（）:：—_-]|工程师|开发岗|岗位|实习生?|校招|社招|秋招)'
    r'(?:[一二三四]面|终面|加面|技术面|HR\s*面)(?=$|[\s｜|·/【】《》()（）:：—_-])',
    re.IGNORECASE,
)
HIRING_CONTEXT = re.compile(r'岗位|工程师|招聘|实习|校招|社招|春招|秋招|开发|算法|后端|\bAgent\b', re.IGNORECASE)
LOGIN_ROUTE = re.compile(r'(?:^|[/.])(?:login|signin|sign-in|passport|sso)(?:$|[/.])', re.IGNORECASE)
LOGIN_NOTICES = ('登录后查看全文', '登录后查看完整', '登录后阅读全文', '请登录后查看', '登录后可查看完整')
NOWCODER_NAV_LINES = frozenset(('牛客网', '牛客网在线编程', '牛客网题解', '牛客企业服务'))
EMPTY_NOWCODER_APP = re.compile(r'<div\b[^>]*\bid=(["\x27])jsApp\1[^>]*>\s*</div>', re.IGNORECASE)


def contains(text, alias):
    if alias.isascii():
        return bool(re.search(rf'(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])', text, re.IGNORECASE))
    return alias in text


def search_target(scope):
    role = ROLE_TERMS.get(scope.get('role'))
    if not role:
        return None
    level = next((level for level in LEVELS if level in scope['question_basis']['text']), '')
    for row in scope.get('jd_coverage', []):
        if row['candidates']:
            continue
        concepts = [label for label, aliases in TECH_TERMS.items()
                    if any(contains(row['quote'], alias) for alias in aliases)][:2]
        if concepts:
            terms = tuple(dict.fromkeys(alias for concept in concepts for alias in TECH_TERMS[concept]))
            query = ' '.join(part for part in (role, level, *concepts, '面经') if part)
            return row['quote'], terms, query
    return None


def post_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (parts.scheme != 'https' or parts.netloc.lower() != POST_HOST
            or not POST_PATH.fullmatch(parts.path)):
        return None
    return f'https://{POST_HOST}{parts.path.rstrip("/")}'


def interview_title(title):
    return ('面经' in title or '面试' in title
            or bool(INTERVIEW_ROUND.search(title) and HIRING_CONTEXT.search(title)))


def ranking_signal(row, title, terms):
    if any(contains(title, term) for term in terms):
        return 2, 'title_topic'
    for field in ('snippet', 'description', 'content'):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            preview = visible_text(value[:2000])
            if any(contains(preview, term) for term in terms):
                return 1, 'snippet_topic'
            break
    return 0, 'vendor_rank'


def post_results(results, terms, diagnostics=None):
    posts = {}
    for rank, row in enumerate(results[:5], start=1):
        url = post_url(row.get('url')) if isinstance(row, dict) else None
        raw_title = row.get('title') if url else None
        title = visible_text(raw_title[:200]) if isinstance(raw_title, str) else ''
        receipt = None
        if diagnostics is not None:
            receipt = {'rank': rank}
            if url:
                receipt['path'] = urlsplit(url).path
            diagnostics.append(receipt)
        if not url:
            reason = 'unsupported_url'
        elif not interview_title(title):
            reason = 'title_filtered'
        elif url in posts:
            reason = 'duplicate_url'
        else:
            priority, signal = ranking_signal(row, title, terms)
            posts[url] = (priority, title[:100], receipt)
            reason = 'page_limit'
            if receipt is not None:
                receipt['ranking_signal'] = signal
        if receipt is not None:
            receipt['selection'] = reason
    ranked = sorted(posts.items(), key=lambda item: item[1][0], reverse=True)
    selected = {}
    for url, (_, title, receipt) in ranked[:PAGE_LIMIT]:
        if receipt is not None:
            receipt['selection'] = 'selected'
        selected[url] = (title, receipt)
    return selected


async def read_body(client, url, content_type, limit, *, method='GET', receipt=None, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
        if receipt is not None:
            receipt['status'] = response.status_code
        if response.status_code != 200:
            if receipt is not None:
                receipt['result'] = 'redirect' if response.is_redirect else 'http_status'
                if response.is_redirect:
                    route = response.headers.get('location', '').split('?', 1)[0].split('#', 1)[0]
                    receipt['redirect_login_hint'] = bool(LOGIN_ROUTE.search(route))
            return None
        if mime != content_type:
            if receipt is not None:
                receipt['result'] = 'unexpected_content_type'
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                if receipt is not None:
                    receipt['result'] = 'body_too_large'
                return None
            body.extend(chunk)
        if receipt is not None:
            receipt['body_bytes'] = len(body)
        return body


def first_fragment(lines, terms, receipt=None):
    fragment = None
    topic_lines = questions = topic_matches = length_matches = eligible = 0
    for line in lines:
        in_range = 16 <= len(line) <= 260
        if receipt is None and not in_range:
            continue
        is_question = bool(QUESTION.search(line))
        if receipt is None and not is_question:
            continue
        on_topic = any(contains(line, term) for term in terms)
        topic_lines += int(on_topic)
        if not is_question:
            continue
        questions += 1
        if not on_topic:
            continue
        topic_matches += 1
        if not in_range:
            continue
        length_matches += 1
        if review_flags(line):
            continue
        if receipt is None:
            return line
        eligible += 1
        if fragment is None:
            fragment = line

    if receipt is not None:
        checks = (
            (len(lines), 'no_visible_text'),
            (questions, 'no_question_marker'),
            (topic_matches, 'no_question_with_topic'),
            (length_matches, 'question_length_filtered'),
            (eligible, 'question_safety_filtered'),
        )
        result = next((reason for count, reason in checks if count == 0), 'retrieved')
        receipt.update(result=result, visible_lines=len(lines), topic_lines=topic_lines, question_lines=questions,
                       topic_question_lines=topic_matches, length_eligible_question_lines=length_matches,
                       eligible_lines=eligible,
                       login_notice=any(marker in line for line in lines for marker in LOGIN_NOTICES))
    return fragment


def context_passages(text, terms):
    """Retain bounded, contiguous post text even when it has no question marker."""
    candidates = []
    start = 0
    while start < len(text):
        end = min(start + PASSAGE_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind('\n', start + PASSAGE_CHARS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        excerpt = text[start:end].strip()
        if 16 <= len(excerpt) and not review_flags(excerpt):
            score = sum(contains(excerpt, term) for term in terms)
            candidates.append((score, excerpt))
        start = end
    return [excerpt for _, excerpt in sorted(candidates, key=lambda candidate: candidate[0],
                                             reverse=True)[:PASSAGES_PER_PAGE]]


class ExperienceLookup:
    """Per-preparation search. Only a verified visible fragment can become a citation."""

    def __init__(self, enabled, key, *, service=None, transport=None, record_diagnostics=False,
                 capture_page=None, semantic_passages=False):
        self.enabled = enabled
        self.key = key
        self.service = service or SearchService('brave', BRAVE_URL)
        self.transport = transport
        self.status = 'not_requested'
        self.sources = {}
        self.delivered_source_ids = set()
        self.target_quote = ''
        self.diagnostics = [] if record_diagnostics else None
        self.citation_checks = Counter() if record_diagnostics else None
        self.capture_page = capture_page
        self.semantic_passages = semantic_passages

    async def lookup(self, scope, admit, *, max_chars=None):
        if self.status == 'not_requested':
            await self._search(scope, admit)
        result = {'status': self.status, 'sources': list(self.sources.values())}
        if result['sources']:
            result['target_quote'] = self.target_quote
        if max_chars is not None:
            while result['sources'] and len(json.dumps(result, ensure_ascii=False)) > max_chars:
                result['sources'].pop()
            if self.sources and not result['sources']:
                result['status'] = 'context_limited'
                result.pop('target_quote')
        self.delivered_source_ids.update(source['id'] for source in result['sources'])
        return result

    async def _search(self, scope, admit):
        if not self.enabled():
            self.status = 'disabled'
            return
        target = search_target(scope)
        if target is None:
            self.status = 'no_target'
            return
        if not self.key or not re.fullmatch(r'[!-~]{1,400}', self.key):
            self.status = 'missing_credentials'
            return
        self.status = 'unavailable'
        if not await admit():
            self.status = 'attempt_already_recorded'
            return
        if not self.enabled():
            self.status = 'disabled'
            return
        self.target_quote, terms, query = target
        try:
            async with asyncio.timeout(6), httpx.AsyncClient(
                    transport=self.transport, follow_redirects=False, trust_env=False) as client:
                method, options = self.service.request(self.key, query, POST_HOST)
                body = await read_body(client, self.service.endpoint, 'application/json',
                                       MAX_SEARCH_BYTES, method=method, **options)
                if body is None:
                    return
                for url, (title, candidate) in post_results(
                        self.service.results(json.loads(body)), terms, self.diagnostics).items():
                    if not self.enabled():
                        break
                    client.cookies.clear()
                    fetch = None
                    if candidate is not None:
                        candidate['fetch'] = fetch = {'result': 'started'}
                    await self._read_post(client, url, title, terms, fetch)
                self.status = 'retrieved' if self.sources else 'no_passage'
        except TimeoutError:
            for candidate in self.diagnostics or ():
                if candidate.get('fetch', {}).get('result') == 'started':
                    candidate['fetch']['result'] = 'search_deadline'
            if self.sources:
                self.status = 'retrieved'
        except (httpx.HTTPError, ValueError):
            if self.sources:
                self.status = 'retrieved'

    async def _read_post(self, client, url, title, terms, receipt):
        try:
            body = await read_body(client, url, 'text/html', MAX_PAGE_BYTES, receipt=receipt,
                                   headers={'Accept': 'text/html'})
            if body is None:
                return
            html = body.decode('utf-8')
            lines = visible_text(html, excluded_tags=PAGE_CHROME).splitlines()
        except httpx.HTTPError:
            if receipt is not None:
                receipt['result'] = 'page_http_error'
            return
        except UnicodeError:
            if receipt is not None:
                receipt['result'] = 'page_decode_error'
            return
        if self.capture_page is not None:
            self.capture_page(url, body, lines)
        if receipt is not None and frozenset(lines) == NOWCODER_NAV_LINES and EMPTY_NOWCODER_APP.search(html):
            first_fragment(lines, terms, receipt)
            receipt['result'] = 'empty_app_shell'
            return
        post_body = (visible_text(html, excluded_tags=PAGE_CHROME, within_tags=('article',),
                                  within_classes=POST_BODY_CLASSES) if self.semantic_passages else '')
        if post_body:
            passages = context_passages(post_body, terms)
            if receipt is not None:
                first_fragment(lines, terms, receipt)
                receipt.update(result='retrieved' if passages else 'no_safe_passage', passage_count=len(passages))
        else:
            fragment = first_fragment(lines, terms, receipt)
            passages = [fragment] if fragment else []
        if not passages:
            return
        content_sha256 = sha256(body).hexdigest()
        fetched_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        for index, excerpt in enumerate(passages):
            key = f'{url}#{index}' if post_body else url
            identity = 'experience-' + sha256(key.encode()).hexdigest()[:20]
            self.sources[identity] = {'id': identity, 'title': title, 'url': url, 'excerpt': excerpt,
                                      'content_sha256': content_sha256, 'fetched_at': fetched_at}

    def citation(self, source_id, quote, target_quote):
        outcome = self._citation_outcome(source_id, quote, target_quote)
        if self.citation_checks is not None:
            self.citation_checks[outcome] += 1
        if outcome == 'verified':
            source = self.sources[source_id]
            return {key: value for key, value in source.items() if key != 'excerpt'} | {'quote': quote}

    def _citation_outcome(self, source_id, quote, target_quote):
        if not source_id and not quote:
            return 'no_citation_claim'
        if not source_id or not quote:
            return 'incomplete_claim'
        if source_id not in self.delivered_source_ids:
            return 'source_not_delivered'
        if not target_quote or target_quote not in self.target_quote:
            return 'different_target'
        if (not 16 <= len(quote) <= 240 or quote not in self.sources[source_id]['excerpt']
                or review_flags(quote)):
            return 'invalid_quote'
        return 'verified'
