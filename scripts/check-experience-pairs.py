"""Evaluate public experience search for frozen synthetic interview cases."""
import argparse
import asyncio
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sys
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from psycopg import AsyncConnection

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))

from interview_agent.api.__main__ import selector_loop
from interview_agent.api.app import create_app
from interview_agent.billing.config import default_policy
from interview_agent.billing.ledger import authorize
from interview_agent.career.repository import digest
from interview_agent.connectors.experience_providers import ENDPOINTS, SearchService
from interview_agent.connectors.experience_settings import credential
from interview_agent.connectors.interview_experiences import MAX_PAGE_BYTES, PAGE_LIMIT, ExperienceLookup, post_url, search_target
from interview_agent.contracts.operations import QueueMessage
from interview_agent.desktop import key_vault
from interview_agent.evaluation.__main__ import provenance, write
from interview_agent.evaluation.adapters import resources
from interview_agent.evaluation.wiki_replay import review_layout
from interview_agent.interview_prep import checkpointer
from interview_agent.interview_prep.billing import PreparationLedger
from interview_agent.interview_prep.planner import LivePlanner
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.interview_prep.worker import worker_for as preparation_worker
from interview_agent.interviews.agent import ScriptedInterviewer
from interview_agent.interviews.billing import InterviewLedger, InterviewPolicy
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for as interview_worker
from interview_agent.storage.config import DatabaseSettings, Settings
from interview_agent.worker.config import WorkerSettings

VERSION = 'f07z177-experience-pairs-v1'
CASES = (
    ('X01', 'ai-application', '负责 Agent 离线评测与失败阶段归因。'),
    ('X02', 'ai-application', '负责 RAG 检索质量评估与重排。'),
    ('X03', 'ai-application', '负责 LangGraph 状态恢复与人工确认。'),
    ('X04', 'java-backend', '负责 Kafka 消费失败的重试与回查。'),
)
CAPS = {'preparation': 600_000_000, 'interview': 400_000_000}
HTTP_LIMITS = {'preparation': 24, 'interview': 40}
MOCK_POST = 'https://www.nowcoder.com/discuss/123456789'
MOCK_EXCERPT = '面试官追问：工具已经选对但离线评测结果仍报错，如何判断是执行环节出了问题？'
X01_BASE_QUERY = 'AI Agent 评测 面经'
X01_FOCUSED_QUERY = 'AI Agent 离线评测 失败归因 面经'
ARCHIVED_VERSION = 'f07z185-archived-experience-blind-v1'
ARCHIVED_CASES = {'X01a': 'baseline-02', 'X01b': 'focused-01'}


class FocusedEvaluationSearch(SearchService):
    def request(self, key, query, site):
        return super().request(key, X01_FOCUSED_QUERY, site)


class SearchTrace(httpx.AsyncBaseTransport):
    def __init__(self, inner, endpoint, counts, events, *, query_limit=4):
        self.inner, self.counts, self.events = inner, counts, events
        self.endpoint = endpoint
        self.limits = {'search': query_limit, 'post': query_limit * PAGE_LIMIT}

    async def handle_async_request(self, request):
        kind = 'search' if str(request.url).split('?', 1)[0] == self.endpoint else 'post'
        self.counts[kind] += 1
        if self.counts[kind] > self.limits[kind]:
            raise RuntimeError('search_request_limit')
        row = {'kind': kind, 'host': request.url.host, 'status': None}
        self.events.append(row)
        response = await self.inner.handle_async_request(request)
        row['status'] = response.status_code
        return response

    async def aclose(self):
        await self.inner.aclose()


def mock_completion(message, *, usage=True):
    return httpx.Response(200, json={
        'id': 'f07z177-mock', 'object': 'chat.completion', 'created': 1, 'model': 'deepseek-flash',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop',
                     'message': {'role': 'assistant', **message}}],
        'usage': {'prompt_tokens': 400, 'completion_tokens': 80, 'total_tokens': 480} if usage else None,
    })


def mock_tools(calls):
    return {'content': None, 'tool_calls': [
        {'id': identifier, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}
        for identifier, name, arguments in calls
    ]}


def mock_planner(request, mode):
    messages = json.loads(request.content)['messages']
    scope = next((json.loads(row['content']) for row in messages if row['role'] == 'tool'), None)
    if scope is None:
        return mock_completion(mock_tools([('scope', 'read_preparation_scope', {})]), usage=mode != 'mock-unknown')
    target = scope['jd_or_direction'].rstrip('。')
    generated = {'title': '岗位场景', 'question': f'针对「{target}」，怎样用一个可重复的实验验证处理结果？',
                 'target_quote': target}
    if sources := scope.get('interview_experiences', {}).get('sources'):
        generated.update(question='如果同一个任务工具已选对但执行仍报错，你怎样定位失败发生的阶段？',
                         experience_id=sources[0]['id'], experience_quote=sources[0]['excerpt'][:200])
    return mock_completion({'content': json.dumps({'generated_questions': [generated]}, ensure_ascii=False)})


def mock_search(service, case_id, request):
    if str(request.url).split('?', 1)[0] == service.endpoint:
        rows = [{'title': '合成面经', 'url': MOCK_POST}] if case_id == 'X01' else []
        result = {'references': rows} if service.provider == 'baidu' else {'web': {'results': rows}}
        return httpx.Response(200, json=result)
    return httpx.Response(200, text=f'<article><p>{MOCK_EXCERPT}</p></article>',
                          headers={'Content-Type': 'text/html'})


def load_archived_pages(directory):
    rows = {item['label']: item for item in json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))}
    pages = {}
    for case_id, label in ARCHIVED_CASES.items():
        row = rows[label]
        url = post_url(row['original_url'])
        with (directory / f'{label}.source.html.txt').open('rb') as raw:
            body = raw.read(MAX_PAGE_BYTES + 1)
        if not url or not body or len(body) > MAX_PAGE_BYTES or sha256(body).hexdigest() != row['sha256']:
            raise ValueError('invalid_archived_public_page')
        pages[case_id] = (url, body)
    return pages


def archived_search(service, page, request):
    url, body = page
    if str(request.url).split('?', 1)[0] == service.endpoint:
        return httpx.Response(200, json={'references': [{'title': '公开面试记录', 'url': url}]})
    if str(request.url) != url:
        raise RuntimeError('archive_replay_unexpected_url')
    return httpx.Response(200, content=body, headers={'Content-Type': 'text/html'})


def write_review(path, mode, pairs):
    lines = ['# 面经搜索对照盲审表', '',
             '同一问题的两侧分别展示实际发布的首题。' if mode == 'live' else '本表来自免费 SDK 模拟。', '',
             '判断贴合考点、可展开的深度、难度以及虚构前提。选 X、Y、平或无效，并记录理由。', '']
    for case_id, jd, question_x, question_y in pairs:
        lines.extend([f'## {case_id}', '', f'岗位要求 · {jd}', '', f'- X · {question_x}',
                      f'- Y · {question_y}', '- 结论 · 待审', '- 理由 · ', ''])
    path.write_text('\n'.join(lines), encoding='utf-8')


def page_capture(out, case_id, variant, saved_pages):
    def save(url, body, lines):
        stem = f'{case_id}-{variant}-{url.rsplit("/", 1)[1]}'
        raw = out / f'{stem}.source.html.txt'
        visible = out / f'{stem}.visible.txt'
        raw.write_bytes(body)
        visible.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        saved_pages.append({'url': url, 'sha256': sha256(body).hexdigest(),
                            'source_file': raw.name, 'visible_file': visible.name})
    return save


async def probe_search(out, service, case=None, *, compare_evaluation_query=False, keep_public_pages=False):
    """Repeat public search without generating or publishing questions."""
    search_key = credential(service)
    if not search_key:
        raise ValueError('search_provider_credential_required')
    out.mkdir(parents=True)
    counts = Counter()
    entries = []
    receipt = {'mode': 'probe-search', 'search_provider': service.provider,
               'search_http': counts, 'model_http': 0, 'cases': entries, 'stop': None}

    async def admit():
        return True

    cases = [row for row in CASES if case is None or row[0] == case]
    trials = [(row, service, 'baseline') for row in cases]
    if compare_evaluation_query:
        trials.append((cases[0], FocusedEvaluationSearch(service.provider, service.endpoint), 'focused'))
        receipt['comparison'] = 'evaluation_query'
    for (case_id, role, jd), trial_service, variant in trials:
        scope = {'role': role, 'question_basis': {'text': jd},
                 'jd_coverage': [{'quote': jd.rstrip('。'), 'candidates': {}}]}
        if compare_evaluation_query:
            target = search_target(scope)
            if target is None or target[2] != X01_BASE_QUERY:
                raise RuntimeError('unexpected_x01_baseline_query')
        events = []
        saved_pages = []
        transport = SearchTrace(httpx.AsyncHTTPTransport(retries=0), trial_service.endpoint, counts, events,
                                query_limit=len(trials))
        capture = page_capture(out, case_id, variant, saved_pages) if keep_public_pages else None
        lookup = ExperienceLookup(lambda: True, search_key, service=trial_service, transport=transport,
                                  record_diagnostics=True, capture_page=capture)
        result = await lookup.lookup(scope, admit)
        entry = {'case_id': case_id, 'status': result['status'], 'source_count': len(result['sources']),
                 'http': events, 'candidates': lookup.diagnostics}
        if compare_evaluation_query:
            entry.update(query_variant=variant, query=X01_FOCUSED_QUERY if variant == 'focused' else X01_BASE_QUERY)
        if keep_public_pages:
            entry['saved_pages'] = saved_pages
        entries.append(entry)
        if result['status'] not in ('retrieved', 'no_passage'):
            receipt['stop'] = result['status']
        write(out / 'search-probe.json', receipt)
        if receipt['stop']:
            break
    print(json.dumps({key: value for key, value in receipt.items() if key != 'cases'}, ensure_ascii=False))
    return 2 if receipt['stop'] else 0


async def execute(args):
    profile = ROOT / '.private'
    out = args.out.resolve()
    if not out.is_relative_to(profile):
        raise ValueError('use_a_fresh_private_receipt_directory')
    live = args.mode == 'live'
    service = SearchService(args.search_provider, ENDPOINTS[args.search_provider])
    if args.mode == 'probe-search':
        return await probe_search(out, service, case=args.case, compare_evaluation_query=args.compare_evaluation_query,
                                  keep_public_pages=args.keep_public_pages)
    archived = args.archived_experiences is not None
    pages = {}
    if archived:
        directory = args.archived_experiences.resolve()
        if not directory.is_relative_to(profile):
            raise ValueError('only_local_private_archives_allowed')
        pages = load_archived_pages(directory)
    cases = tuple((case, CASES[0][1], CASES[0][2]) for case in ARCHIVED_CASES) if archived else CASES
    version = ARCHIVED_VERSION if archived else VERSION
    caps = dict.fromkeys(CAPS, 300_000_000) if archived else CAPS
    limits = {'preparation': 6 * len(cases), 'interview': 10 * len(cases)} if archived else HTTP_LIMITS
    search_key = credential(service) if live and not archived else 'synthetic-search-key'
    if live and (not search_key or not key_vault.load()):
        raise ValueError('both_provider_credentials_required')
    api = Settings.load(profile / 'postgres/test.json')
    settings = WorkerSettings.load(profile / 'redis/test-worker.json')
    owner = DatabaseSettings.model_validate_json(
        (profile / 'postgres/test-migration.json').read_text(encoding='utf-8'))
    if (api.mode != 'test' or settings.mode != 'test' or api.database != settings.database
            or api.database.dbname != 'py_interview_test' or api.assessment_mode != 'scripted'
            or (owner.host, owner.port, owner.dbname) != (api.database.host, api.database.port, api.database.dbname)):
        raise ValueError('dedicated_test_database_required')
    out.mkdir(parents=True)
    ids = {stage: f'{version}-{stage}' + ('' if live else f'-{uuid4().hex}') for stage in caps}
    policies = {stage: InterviewPolicy.model_validate({**default_policy().model_dump(),
                'max_http_attempts': limits[stage]}) for stage in caps}
    credentials = 'vault' if live else out / 'mock-model.env'
    if not live:
        credentials.write_text('DEEPSEEK_API_KEY=offline-not-real\n', encoding='utf-8')
    layout = review_layout(version, [case[0] for case in cases])
    write(out / 'plan.json', {'mode': args.mode, 'search_provider': service.provider,
          'cases': cases, 'cases_sha256': digest(cases), 'archived_pages': {
              case: sha256(page[1]).hexdigest() for case, page in pages.items()},
          'controller_sha256': sha256(Path(__file__).read_bytes()).hexdigest(), 'authorization_ids': ids,
          'policies': {stage: policy.digest for stage, policy in policies.items()}, 'max_nano': caps,
          'max_model_http': limits, 'max_search_http': 0 if archived else 4,
          'max_post_http': 0 if archived else 8, 'run': provenance(ROOT)})
    pairs = []
    review = []
    counts = Counter()
    stop = None
    async with resources(settings) as (pool, queue):
        prep_repo, int_repo = PreparationRepository(pool), InterviewRepository(pool)
        prep_ledger = PreparationLedger(pool, policies['preparation'], ids['preparation'], prep_repo)
        int_ledger = InterviewLedger(pool, policies['interview'], ids['interview'])
        async with await AsyncConnection.connect(owner.conninfo()) as conn:
            existing = await (await conn.execute(
                'SELECT id FROM app.model_authorizations WHERE id=ANY(%s)', (list(ids.values()),))).fetchone()
            if existing:
                raise ValueError('authorization_already_used_no_retry')
        for stage, cap in caps.items():
            await authorize(owner, policies[stage], ids[stage], cap)

        async def pump(worker, operation, job, version):
            await queue.publish(QueueMessage(operation_id=operation, job_id=job, resource_version=version))
            if not await worker.once():
                raise RuntimeError('owned_job_not_received')

        def preparations(factory=None):
            return preparation_worker(prep_repo, queue, pool=pool,
                saver_factory=lambda: checkpointer.saver(settings.database), planner_factory=factory)

        with TestClient(create_app(api), base_url=api.origin,
                        backend_options={'loop_factory': selector_loop}) as client:
            client.headers['Origin'] = api.origin

            def request(path, body=None):
                response = client.get(path) if body is None else client.post(
                    path, json=body, headers={'Idempotency-Key': uuid4().hex})
                if response.status_code >= 400:
                    raise RuntimeError(f"api_{response.status_code}_{response.json().get('code', 'unknown')}")
                return response.json()

            try:
                for index, (case_id, role, jd) in enumerate(cases):
                    user = request('/api/auth/sign-up/email', {'name': 'Experience search synthetic',
                        'email': f'f177-{uuid4().hex}@desktop.example.com',
                        'password': secrets.token_urlsafe(24)})['user']['id']
                    pair = {'case_id': case_id, 'user_id': user, 'arms': {}}
                    pairs.append(pair)
                    for arm in ('B', 'C') if index % 2 == 0 else ('C', 'B'):
                        item = {'search_http': []}
                        pair['arms'][arm] = item
                        goal = request('/api/career-goals/', {'message': '合成首题评测',
                            'request': 'save_jd', 'jd_text': jd,
                            'conditions': {'direction': 'AI 应用开发' if role == 'ai-application' else 'Java 后端'}})
                        root = f"/api/career-goals/{goal['id']}"
                        prep = request(root + '/interview-preparations', {'expected_version': goal['version'],
                            'profile': 'full', 'use_resume': False, 'role': role})
                        prep_id = prep['id']
                        item.update(goal_id=goal['id'], preparation_id=prep_id)
                        prep_path = root + '/interview-preparations/' + prep_id
                        lookup = None
                        if archived or arm == 'C':
                            if archived:
                                transport = httpx.MockTransport(lambda request: archived_search(service, pages[case_id], request))
                            else:
                                transport = (httpx.AsyncHTTPTransport(retries=0) if live
                                             else httpx.MockTransport(lambda request: mock_search(service, case_id, request)))
                            lookup = ExperienceLookup(lambda: True, search_key, service=service,
                                transport=SearchTrace(transport, service.endpoint, counts, item['search_http']),
                                record_diagnostics=True, semantic_passages=archived and arm == 'C')
                            item['search_candidates'] = lookup.diagnostics

                        def planner(claim):
                            return LivePlanner(pool, prep_repo, claim, policies['preparation'], ids['preparation'],
                                credentials, experience_lookup=lookup,
                                inner_factory=None if live else
                                lambda: httpx.MockTransport(lambda request: mock_planner(request, args.mode)))

                        await pump(preparations(planner), prep_id, prep_id, prep['version'])
                        prepared = request(prep_path)
                        cost = await prep_ledger.summary(prep_id)
                        item.update(preparation=prepared, preparation_fee=cost)
                        if cost['usage_status'] != 'known' or prepared['status'] != 'WAITING_FOR_USER':
                            raise RuntimeError('preparation_' + (prepared.get('error_code') or cost['usage_status']))
                        async with pool.connection() as conn:
                            row = await (await conn.execute(
                                'SELECT frozen FROM app.career_interview_preparations WHERE id=%s', (prep_id,))).fetchone()
                        frozen = row['frozen']
                        item['preparation_input_sha256'] = digest(frozen)
                        if search_target(frozen) is None:
                            raise RuntimeError('fixture_missing_search_gap')
                        if lookup and lookup.status not in ('retrieved', 'no_passage'):
                            raise RuntimeError('search_' + lookup.status)
                        approved = request(prep_path + '/confirm', {'expected_version': prepared['version'],
                            'plan_sha256': prepared['plan_sha256'], 'decision': 'approve'})
                        await pump(preparations(), prep_id, prep_id, approved['version'])
                        opened = request(prep_path)
                        if opened['status'] != 'SUCCEEDED':
                            raise RuntimeError('opening_' + (opened.get('error_code') or 'not_succeeded'))
                        sid = opened['session_id']
                        item['session_id'] = sid
                        session_path = f'{root}/interviews/{sid}'
                        async with pool.connection() as conn:
                            run = await (await conn.execute(
                                "SELECT id,job_id,goal_version,input_payload FROM app.career_interview_runs "
                                "WHERE session_id=%s AND status='PENDING'", (sid,))).fetchone()
                        if run is None:
                            raise RuntimeError('expected_owned_pending_interview')

                        mock_factory = None
                        if not live:
                            async def admit():
                                pass  # The Worker reserves each mocked SDK request.

                            adapter = ScriptedInterviewer(run['input_payload'], admit)

                            async def mock_interview(_request):
                                answer = await adapter.complete([], [])
                                return mock_completion(mock_tools([(call.id, call.name, call.arguments)
                                                                   for call in answer.tool_calls]))

                            mock_factory = lambda: httpx.MockTransport(mock_interview)

                        worker = interview_worker(int_repo, queue, provider='live', policy=policies['interview'],
                            authorization=ids['interview'], credentials=credentials,
                            inner_factory=mock_factory)
                        await pump(worker, str(run['id']), str(run['job_id']), run['goal_version'])
                        first = request(session_path)
                        item['interview_fee'] = await int_ledger.summary(str(run['id']))
                        item['run'] = first['run']
                        actual = [event['payload'] for event in first['events'] if event['kind'] == 'question']
                        if (item['interview_fee']['usage_status'] != 'known' or first['status'] != 'WAITING_FOR_USER'
                                or len(actual) != 1 or actual[0]['kind'] != 'base'):
                            raise RuntimeError('first_question_unavailable_or_unsettled')
                        ended = request(session_path + '/stop', {'expected_version': first['version']})
                        item.update(question=actual[0], report=ended['report'])
                    if len({item['preparation_input_sha256'] for item in pair['arms'].values()}) != 1:
                        raise RuntimeError('pair_preparation_input_mismatch')
                    x, y = layout[case_id]
                    pair['review_order'] = [x, y]
                    review.append((case_id, jd, pair['arms'][x]['question']['text'], pair['arms'][y]['question']['text']))
                    write(out / 'report.json', {'mode': args.mode, 'pairs': pairs, 'stop': None})
            except (RuntimeError, ValueError) as error:
                stop = str(error)
            finally:
                fees = {'preparation': await prep_ledger.summary(), 'interview': await int_ledger.summary()}
                summary = {'mode': args.mode, 'completed_pairs': len(review), 'stop': stop,
                           'search_http': {} if archived else dict(counts),
                           **({'archived_replay_requests': dict(counts)} if archived else {}),
                           'model_http': sum(fee['http_attempts'] for fee in fees.values()),
                           'accounted_upper_nano': sum(fee['accounted_upper_nano'] for fee in fees.values())}
                write(out / 'report.json', {**summary, 'pairs': pairs})
                write(out / 'fees.json', fees)
                write_review(out / 'review-pairs.md', args.mode, review)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if len(review) == len(cases) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mode', choices=('mock', 'mock-unknown', 'live', 'probe-search'), default='mock')
    parser.add_argument('--search-provider', choices=ENDPOINTS, default='baidu')
    parser.add_argument('--case', choices=[row[0] for row in CASES], help='select one fixed probe-search case')
    parser.add_argument('--compare-evaluation-query', action='store_true', help='compare two fixed X01 queries')
    parser.add_argument('--keep-public-pages', action='store_true', help='save read public pages in the probe directory')
    parser.add_argument('--archived-experiences', type=Path, help='compare old and new extraction with fixed local HTML')
    parser.add_argument('--execute-live', action='store_true')
    args = parser.parse_args()
    if (args.mode in ('live', 'probe-search')) != args.execute_live:
        parser.error('live_requires_explicit_flag_and_stage_authorization')
    if args.case and args.mode != 'probe-search':
        parser.error('case_filter_requires_probe_search_mode')
    if args.compare_evaluation_query and (args.mode, args.search_provider, args.case) != ('probe-search', 'baidu', 'X01'):
        parser.error('evaluation_query_comparison_requires_baidu_x01_probe')
    if args.keep_public_pages and args.mode != 'probe-search':
        parser.error('public_page_capture_requires_probe_search_mode')
    if args.archived_experiences and (args.mode == 'probe-search' or args.search_provider != 'baidu'):
        parser.error('archived_experiences_requires_paired_mode_and_baidu_format')
    return asyncio.run(execute(args), loop_factory=selector_loop)


if __name__ == '__main__':
    raise SystemExit(main())
