"""Run a fixed frozen suite through budgeted B/C interview Worker decisions."""
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
from interview_agent.connectors.public_reference import CATALOG
from interview_agent.contracts.operations import QueueMessage
from interview_agent.evaluation.__main__ import provenance, write
from interview_agent.evaluation.adapters import resources
from interview_agent.evaluation.wiki_replay import load_suite, review_layout, review_markdown
from interview_agent.interviews.agent import ScriptedInterviewer
from interview_agent.interviews.billing import InterviewLedger, InterviewPolicy
from interview_agent.interviews.contracts import CreateInterview
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for
from interview_agent.materials.repository import MaterialRepository
from interview_agent.storage.config import DatabaseSettings, Settings
from interview_agent.worker.config import WorkerSettings

PILOT = ('W01', 'W06', 'W12')
SUITES = {
    'pilot': ('wiki-replay-v0.1', 'f07z172-wiki-pilot', 30),
    'holdout': ('wiki-replay-holdout-v0.1', 'f07z173-wiki-holdout', 96),
}
MAX_NANO = 500_000_000
WIKI_PATH = '/api/runtime/interview-wiki'


def candidate(title, question, probe='', reference=None):
    return {'title': title, 'question': question, 'probe': probe, 'target_quote': '',
            'kind': 'scenario', 'difficulty': 'intermediate',
            **({'public_reference': reference} if reference else {})}


def common_scope(scope):
    """Remove arm-local event IDs, clock and the single deliberately added Wiki item."""
    value = dict(scope)
    if scope['answer']['question_id'] != scope['current_question']['id']:
        raise ValueError('answer_not_bound_to_frozen_question')
    value['current_question'] = {k: v for k, v in scope['current_question'].items() if k != 'id'}
    value['answer'] = {k: v for k, v in scope['answer'].items() if k not in ('id', 'question_id')}
    value['question_history'] = [value['current_question']]
    value['answers'] = [value['answer']]
    value.pop('remaining_seconds', None)
    value.pop('wiki_version_id', None)
    pack = dict(scope['pack'])
    pack.pop('sha256', None)
    pack['topics'] = {key: topic for key, topic in pack['topics'].items() if not topic.get('wiki')}
    pack['topic_order'] = [key for key in pack['topic_order'] if key in pack['topics']]
    value['pack'] = pack
    return value


async def mock_response(scripted, unknown):
    answer = await scripted.complete([], [])
    calls = [{'id': call.id, 'type': 'function', 'function': {
        'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)}}
        for call in answer.tool_calls]
    return httpx.Response(200, json={
        'id': f'f172-mock-{scripted.n}', 'object': 'chat.completion', 'created': 1,
        'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
            'message': {'role': 'assistant', 'content': '', 'tool_calls': calls}}],
        'usage': None if unknown else {'prompt_tokens': 100, 'completion_tokens': 25,
                                      'total_tokens': 125},
    })


class TopicTransport(httpx.AsyncBaseTransport):
    """Record delivered topic IDs only; never keep HTTP headers or private reasoning."""
    def __init__(self, inner, topics):
        self.inner = inner
        self.topics = topics

    async def handle_async_request(self, request):
        payload = json.loads(await request.aread())
        for message in payload.get('messages', []):
            if message['role'] != 'tool':
                continue
            try:
                data = json.loads(message['content'])
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                self.topics.update(row['id'] for row in data.get('topics', []) if 'id' in row)
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


async def execute(args):
    suite_name, authorization, max_http = SUITES[args.suite]
    manifest, all_cases, _ = load_suite(ROOT / 'benchmarks' / suite_name)
    cases = {case.id: case for case in all_cases}
    selected = PILOT if args.suite == 'pilot' else tuple(cases)
    layout = review_layout(manifest['version'], selected)
    profile = ROOT / '.private'
    api = Settings.load(profile / 'postgres/test.json')
    settings = WorkerSettings.load(profile / 'redis/test-worker.json')
    owner = DatabaseSettings.model_validate_json(
        (profile / 'postgres/test-migration.json').read_text(encoding='utf-8'))
    if (api.mode != 'test' or settings.mode != 'test' or api.database != settings.database
            or api.database.dbname != 'py_interview_test' or api.assessment_mode != 'scripted'
            or (owner.host, owner.port, owner.dbname) != (api.database.host, api.database.port, api.database.dbname)):
        raise ValueError('dedicated_test_database_required')
    policy = InterviewPolicy.model_validate({**default_policy().model_dump(), 'max_http_attempts': max_http})
    live = args.mode == 'live'
    nonce = uuid4().hex
    identity = authorization if live else f'{authorization}-mock-{nonce}'
    out = args.out.resolve()
    if not out.is_relative_to(profile):
        raise ValueError('use_a_fresh_private_receipt_directory')
    out.mkdir(parents=True)
    credentials = 'vault' if live else out / 'mock-credentials.env'
    if not live:
        credentials.write_text('DEEPSEEK_API_KEY=offline-not-real\n', encoding='utf-8')
    write(out / 'plan.json', {
        'mode': args.mode, 'authorization': identity, 'cases': list(selected), 'arms': ['B', 'C'],
        'policy_sha256': policy.digest, 'max_nano': MAX_NANO, 'max_http': max_http,
        'suite': manifest, 'controller_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'run': provenance(ROOT),
    })
    report = {'mode': args.mode, 'pairs': [], 'stop': None, 'human_review_total': 0}
    reviews = []
    synthetic_source = {'id': 'langgraph-checkpoints', 'title': '合成资料标记',
                        'url': CATALOG['langgraph-checkpoints'].url,
                        'quote': '仅用于锁定来源可用的实验变量，正文未参与评分。',
                        'content_sha256': digest('f172-synthetic-source'), 'fetched_at': '2026-11-23T00:00:00+00:00'}
    async with resources(settings) as (pool, queue):
        repo = InterviewRepository(pool)
        ledger = InterviewLedger(pool, policy, identity)
        async with await AsyncConnection.connect(owner.conninfo()) as conn:
            existing = await (await conn.execute(
                'SELECT id FROM app.model_authorizations WHERE id=%s', (identity,))).fetchone()
            if existing:
                raise ValueError('authorization_already_used_no_retry')
        await authorize(owner, policy, identity, MAX_NANO)
        with TestClient(create_app(api), base_url=api.origin,
                        backend_options={'loop_factory': selector_loop}) as client:
            client.headers['Origin'] = api.origin

            def request(path, body=None):
                response = client.get(path) if body is None else client.post(
                    path, json=body, headers={'Idempotency-Key': uuid4().hex})
                view = response.json()
                if response.status_code >= 400:
                    raise RuntimeError(f"api_{response.status_code}_{view.get('code','unknown')}")
                return view

            async def new_session(user, case, questions):
                goal = request('/api/career-goals/', {'message': '合成评测，不执行岗位研究。', 'jd_text': case.jd,
                                                      'conditions': {'direction': 'AI 应用开发'}})
                async with pool.connection() as conn:
                    row = await (await conn.execute(
                        'SELECT * FROM app.career_goals WHERE id=%s AND user_id=%s', (goal['id'], user))).fetchone()
                    target = await MaterialRepository(pool)._target(conn, row)
                session, _ = await repo.create_for_owner(
                    user, goal['id'], uuid4().hex, CreateInterview(expected_version=goal['version'],
                        role='ai-application', mode='interview', use_resume=False, max_questions=4),
                    generated_questions=questions, generated_target_sha256=digest(target))
                return session, f"/api/career-goals/{goal['id']}/interviews/{session}"

            async def pending(session):
                async with pool.connection() as conn:
                    row = await (await conn.execute('SELECT * FROM app.career_interview_runs '
                        "WHERE session_id=%s AND status='PENDING'", (session,))).fetchone()
                    if row is None:
                        raise RuntimeError('expected_owned_pending_run')
                    return row

            async def pump(row, **kwargs):
                message = QueueMessage(operation_id=str(row['id']), job_id=str(row['job_id']),
                                       resource_version=row['goal_version'])
                await queue.publish(message)
                if not await worker_for(repo, queue, **kwargs).once():
                    raise RuntimeError('owned_job_not_received')

            try:
                for index, case_id in enumerate(selected):
                    case = cases[case_id]
                    user = request('/api/auth/sign-up/email', {'name': 'Wiki replay synthetic',
                        'email': f'f172-{nonce}-{case_id.lower()}@desktop.example.com',
                        'password': secrets.token_urlsafe(24)})['user']['id']
                    pair = {'case_id': case_id, 'user_id': user, 'attempts': {}}
                    report['pairs'].append(pair)
                    wiki = None
                    if case.wiki.status == 'active':
                        seed, seed_url = await new_session(user, case, [candidate(case.wiki.title,
                            case.wiki.question, reference=synthetic_source)])
                        await pump(await pending(seed), provider='scripted')
                        seed_view = request(seed_url)
                        request(seed_url + '/stop', {'expected_version': seed_view['version']})
                        [draft] = request(WIKI_PATH)['items']
                        published = request(f"{WIKI_PATH}/{draft['id']}/review",
                                            {'action': 'publish', 'expected_active_id': None})
                        wiki = {**published['document'], 'target_quote': '',
                                'wiki': {key: published[key] for key in ('id', 'version', 'topic_key')}}

                    arms = {}
                    for arm in ('B', 'C'):
                        questions = [candidate('本轮已问', case.question, case.probe)]
                        if arm == 'C' and wiki:
                            questions.append(wiki)
                        questions.append(candidate('继续同一技术主题', case.baseline,
                                                   reference=synthetic_source))
                        sid, url = await new_session(user, case, questions)
                        await pump(await pending(sid), provider='scripted')
                        view = request(url)
                        [first] = [event for event in view['events'] if event['kind'] == 'question']
                        if first['payload']['text'] != case.question or view['status'] != 'WAITING_FOR_USER':
                            raise RuntimeError('frozen_first_question_mismatch')
                        request(url + '/answers', {'expected_version': view['version'],
                                                  'question_id': first['id'], 'text': case.answer})
                        arms[arm] = (sid, url, await pending(sid))
                    b, c = (common_scope(arms[arm][2]['input_payload']) for arm in ('B', 'C'))
                    if b != c:
                        raise RuntimeError('shared_pair_input_mismatch')
                    pair['shared_input_sha256'] = digest(b)
                    pair['order'] = ['B', 'C'] if index % 2 == 0 else ['C', 'B']
                    for arm in pair['order']:
                        sid, url, row = arms[arm]
                        scope = row['input_payload']
                        topics = set()

                        async def admit():
                            pass  # Real admissions are enforced by the worker's ledger.

                        script = ScriptedInterviewer(scope, admit)

                        async def mock(_request):
                            return await mock_response(script, args.mode == 'mock-unknown')

                        transport = httpx.AsyncHTTPTransport(retries=0) if live else httpx.MockTransport(mock)
                        attempt = {'session_id': sid, 'run_id': str(row['id']), 'input_sha256': digest(scope)}
                        pair['attempts'][arm] = attempt
                        write(out / 'report.json', report)
                        await pump(row, provider='live', policy=policy, authorization=identity,
                                   credentials=credentials, inner_factory=lambda: TopicTransport(transport, topics))
                        fees = await ledger.summary(str(row['id']))
                        view = request(url)
                        result = [event for event in view['events'] if event['kind'] == 'question']
                        wiki_ids = {key for key, topic in scope['pack']['topics'].items() if topic.get('wiki')}
                        chose = result[1]['payload'] if len(result) == 2 else None
                        attempt.update(status=view['status'], question=chose, run=view['run'],
                                       wiki_offered=bool(wiki_ids), wiki_read=bool(topics & wiki_ids),
                                       wiki_used=bool(chose and chose['topic_id'] in wiki_ids),
                                       topic_reads=sorted(topics), fees=fees)
                        write(out / 'report.json', report)
                        if fees['usage_status'] != 'known' or not chose or chose['kind'] == 'project':
                            report['stop'] = ('unknown_usage' if fees['usage_status'] == 'unknown'
                                              else 'decision_invalid_or_unavailable')
                            break
                        request(url + '/stop', {'expected_version': view['version']})
                    if report['stop']:
                        for arm, (_, url, _) in arms.items():
                            if arm not in pair['attempts']:
                                remaining = request(url)
                                request(url + '/stop', {'expected_version': remaining['version']})
                        break
                    left, right = layout[case_id]
                    reviews.append({'case_id': case.id, 'jd': case.jd, 'previous_question': case.question,
                        'answer': case.answer, 'question_x': pair['attempts'][left]['question']['text'],
                        'question_y': pair['attempts'][right]['question']['text']})
                    pair['review_arm_order'] = [left, right]
            except (RuntimeError, ValueError) as error:
                report['stop'] = str(error)
            finally:
                fees = await ledger.summary()
                report['total'] = {
                    'completed_pairs': len(reviews), 'planned_pairs': len(selected),
                    'http_attempts': fees['http_attempts'], 'real_model_http': fees['http_attempts'] if live else 0,
                    'accounted_upper_nano': fees['accounted_upper_nano'],
                    'usage_status': fees['usage_status'],
                    'wiki_outcomes': dict(Counter(
                        ('used' if row['wiki_used'] else 'read_not_used' if row['wiki_read']
                         else 'offered_not_read' if row['wiki_offered'] else 'withheld')
                        for pair in report['pairs'] for arm, row in pair['attempts'].items()
                        if arm == 'C' and row.get('question'))),
                }
                write(out / 'fees.json', fees)
                write(out / 'report.json', report)
                source = '同一真实模型' if live else '同一 SDK 脚本模拟'
                (out / 'review-pairs.md').write_text(review_markdown(reviews, source=source), encoding='utf-8')
    print(json.dumps({'stop': report['stop'], **report['total']}, ensure_ascii=False))
    return 0 if len(reviews) == len(selected) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=SUITES, default='pilot')
    parser.add_argument('--mode', choices=['mock', 'mock-unknown', 'live'], default='mock')
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if (args.mode == 'live') != args.execute_live:
        parser.error('live_requires_explicit_flag_and_stage_authorization')
    return asyncio.run(execute(args), loop_factory=selector_loop)


if __name__ == '__main__':
    raise SystemExit(main())
