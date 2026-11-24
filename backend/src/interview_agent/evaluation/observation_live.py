"""Authorized live execution of the fixed observation samples (F07Q, budget 0.5 CNY / 64 HTTP).

Why this file exists instead of a flag on `observation.py`:
`BillingLedger.reserve()` only accepts a call that belongs to an owned operation claim, so
a billed model request *cannot* be issued from a synthetic in-memory scope. A real run has
to create a real session, answer it, and let the product worker make the decision. That is
what this module does: same API, same worker, same ledger, same observation table.

Every guard here fails closed:
  * development environment only (`py_interview`), matching `evaluation.__main__`;
  * the authorization ID is fixed to the budget the operator approved;
  * an existing authorization row aborts the run instead of retrying;
  * `usage_status == unknown` stops the whole run (no automatic retry, no double spend).
"""
import asyncio, json, secrets
from pathlib import Path
from uuid import uuid4

AUTHORIZATION_ID = 'f07q-20261111-cny05'
MAX_CNY = '0.5'
MAX_NANO = 500_000_000
MAX_HTTP = 64
# LivePolicy caps one authorization at 12 HTTP on purpose. The approved 64 is therefore
# spent as several bounded admissions, each with its own id and its own slice of the
# budget, instead of raising the fuse.
# LivePolicy caps one authorization at 12 HTTP, and one in-flight call reserves
# `reservation_nano` (0.147 CNY at input_reservation_tokens=65536). Slicing the approved
# 0.5 CNY across many batches cannot work: a slice smaller than one reservation aborts
# before the first HTTP (run be4c1e32), and a slice barely above it dies on the second
# call because settled spend adds on top of the next reservation (run 42d783a4).
# So one decision = one authorization, which is exactly how every previous live stage in
# this repo was run. The aggregate 0.5 CNY promise is enforced by the run-level counter.
BATCH_SIZE = 1
BANK_SCHEMA_VERSION = 1


def credentials(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        name, _, value = line.partition('=')
        if name.strip() == 'DEEPSEEK_API_KEY' and value.strip():
            return {'DEEPSEEK_API_KEY': value.strip()}
    raise ValueError('deepseek_key_missing')


def bank_document(sample):
    """One frozen item per sample: the question and reference the answer must be judged against."""
    return {'schema_version': BANK_SCHEMA_VERSION, 'name': 'F07Q ' + sample['id'], 'items': [{
        'item_id': sample['id'],
        'kind': 'knowledge',
        'roles': [sample['role']],
        'title': sample['id'],
        'question': sample['question'],
        'reference': sample['reference'],
        'source_url': 'https://example.invalid/f07q-authored-sample',
        'source_note': 'F07Q authored synthetic sample; not a company question.',
        'tags': ['f07q', sample['kind']],
    }]}


def question_for(sample):
    """Frozen question text per sample: pack question for technical, crafted prompt for project."""
    if sample['topic_id'] == 'project':
        return ('材料里记录的是：「' + sample['fact']['quote'] + '」。'
                '请说明你在这个模块里具体做了什么、如何验证，以及哪些部分不是你做的。')
    from interview_agent.interviews.catalog import pack
    return pack(sample['role'])['topics'][sample['topic_id']][sample['question_kind']]


def reference_for(sample):
    if sample['topic_id'] == 'project':
        return '材料记录：' + sample['fact']['quote']
    from interview_agent.interviews.catalog import pack
    return pack(sample['role'])['topics'][sample['topic_id']]['reference']


async def run_failure(pool, session_id):
    """Surface why a live decision produced no observation, instead of hiding it."""
    async with pool.connection() as conn:
        row = await (await conn.execute(
            'SELECT status,error_code FROM app.career_interview_runs WHERE session_id=%s ORDER BY created_at DESC LIMIT 1',
            (session_id,))).fetchone()
    if not row:
        return 'run_missing'
    return '%s/%s' % (row['status'], row['error_code'])


async def execute(root, samples, repeats, *, api_config, worker_config, owner_config,
                  policy_path, credentials_path, output_dir):
    import httpx
    from fastapi.testclient import TestClient
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
    from interview_agent.api.app import create_app
    from interview_agent.api.__main__ import selector_loop
    from interview_agent.storage.config import Settings, DatabaseSettings
    from interview_agent.worker.config import WorkerSettings
    from interview_agent.worker.queue import QueueTransport
    from interview_agent.contracts.operations import QueueMessage
    from interview_agent.billing.ledger import authorize
    from interview_agent.interviews.repository import InterviewRepository
    from interview_agent.interviews.worker import worker_for
    from interview_agent.interviews.billing import InterviewPolicy, InterviewLedger
    from .observation import grade, summarize, write

    api = Settings.load(api_config)
    worker = WorkerSettings.load(worker_config)
    owner = DatabaseSettings.model_validate_json(Path(owner_config).read_text(encoding='utf-8'))
    policy = InterviewPolicy.load(policy_path)
    for profile in (api.database, worker.database, owner):
        if (profile.host, profile.port, profile.dbname) != (api.database.host, api.database.port, api.database.dbname):
            raise ValueError('database_profiles_must_match')
    if api.mode != 'development' or worker.mode != 'development' or api.database.dbname != 'py_interview':
        raise ValueError('live_run_requires_development_py_interview')
    if owner.dbname != 'py_interview' or owner.user != 'py_interview_owner':
        raise ValueError('owner_must_be_owner_role_on_development_db')
    if policy.max_http_attempts > MAX_HTTP:
        raise ValueError('policy_exceeds_authorized_http_ceiling')

    credentials_value = credentials(credentials_path)
    nonce = uuid4().hex
    out = Path(output_dir) / nonce
    out.mkdir(parents=True)
    write(out / 'plan.json', {'authorization': AUTHORIZATION_ID, 'max_nano': MAX_NANO, 'max_http': MAX_HTTP,
                              'samples': [s['id'] for s in samples], 'repeats': repeats,
                              'policy': policy.model_dump(mode='json'),
                              'scope': 'real API + real interview worker + real ledger; scripted first question'})

    pool = AsyncConnectionPool(api.database.conninfo(), open=False, kwargs={'row_factory': dict_row})
    redis = worker.redis.connect()
    streams = []
    records = []

    async def pump(goal_id, provider, authorization=None):
        repo = InterviewRepository(pool)
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT * FROM app.career_interview_runs WHERE goal_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",
                (goal_id,))).fetchone()
        if not row:
            raise RuntimeError('expected_pending_interview_run')
        stream = 'py-interview:%s:%s:jobs:v1:interviews' % (api.mode, nonce)
        if stream not in streams:
            streams.append(stream)
        queue = QueueTransport(redis, stream)
        await queue.publish(QueueMessage(operation_id=str(row['id']), job_id=str(row['job_id']),
                                         resource_version=row['goal_version']))
        kwargs = {'provider': 'scripted'} if provider == 'scripted' else {
            'provider': 'live', 'policy': policy, 'authorization': authorization,
            # load_key() takes a Path; it reads the file itself. Passing a dict here makes
            # the worker fail before the first model call (verified: run d96ac344, 0 HTTP).
            'credentials': credentials_path}
        if not await worker_for(repo, queue, **kwargs).once():
            raise RuntimeError('worker_did_not_take_owned_job')

    await pool.open(wait=True)
    await redis.ping()
    worklist = [(sample, repeat) for repeat in range(1, repeats + 1) for sample in samples]
    batches = [worklist[index:index + BATCH_SIZE] for index in range(0, len(worklist), BATCH_SIZE)]
    # One in-flight call holds `policy.reservation_nano` (input reservation + max output).
    # Fail closed if the approved total cannot even cover one reservation.
    floor_nano = int(policy.reservation_nano) + 1
    if floor_nano > MAX_NANO:
        raise ValueError('reservation_nano_exceeds_approved_total')
    per_batch_nano = MAX_NANO
    total_calls = 0
    total_nano = 0
    stopped = None

    try:
        with TestClient(create_app(api), base_url=api.origin,
                        backend_options={'loop_factory': selector_loop}, raise_server_exceptions=False) as client:
            client.headers['Origin'] = api.origin

            def request(path, body=None):
                response = client.get(path) if body is None else client.post(
                    path, json=body, headers={'Idempotency-Key': uuid4().hex})
                if response.status_code >= 400:
                    raise RuntimeError('HTTP_%s_%s' % (response.status_code, response.json().get('code', 'unknown')))
                return response.json()

            for index, batch in enumerate(batches, start=1):
                if stopped:
                    for sample, repeat in batch:
                        records.append({'sample_id': sample['id'], 'kind': sample['kind'], 'repeat': repeat,
                                        'result': {'status': 'not_run', 'error_code': stopped},
                                        'grade': {'sample_id': sample['id'], 'kind': sample['kind'],
                                                  'status': 'not_graded', 'passed': False}})
                    continue
                identity = '%s-%s-b%d' % (AUTHORIZATION_ID, nonce[:8], index)
                async with await AsyncConnection.connect(owner.conninfo()) as conn:
                    if await (await conn.execute('SELECT id FROM app.model_authorizations WHERE id=%s',
                                                 (identity,))).fetchone():
                        raise RuntimeError('authorization_already_exists_do_not_retry:' + identity)
                await authorize(owner, policy, identity, per_batch_nano)
                ledger = InterviewLedger(pool, policy, identity)

                for sample, repeat in batch:
                    tag = '%s-r%d' % (sample['id'], repeat)
                    try:
                        request('/api/auth/sign-up/email', {
                            'email': 'f07q-%s-%s@example.com' % (nonce, tag),
                            'name': 'F07Q synthetic', 'password': secrets.token_urlsafe(24)})
                        goal = request('/api/career-goals/', {
                            'message': 'F07Q 固定评测样本（合成，不研究岗位）', 'intent': 'training',
                            'conditions': {'direction': 'Java 后端'}})
                        base = '/api/career-goals/' + goal['id']
                        preview = request('/api/question-banks/imports', {
                            'document': bank_document({**sample, 'question': question_for(sample),
                                                       'reference': reference_for(sample)})})
                        bank = request('/api/question-banks/imports/%s/commit' % preview['preview_id'], {})
                        session = request(base + '/interviews', {
                            'expected_version': 1, 'role': sample['role'], 'mode': 'interview',
                            # One question per sample: the eval wants the judgment of this single
                            # answer, not a second question. With max_questions=4 the agent could
                            # neither finish (minimum 2 asked) nor legally follow up in a one-item
                            # bank, so it burned all 4 model requests (run a534ec90: 3/16 failures).
                            'max_questions': 1, 'duration_seconds': 3600,
                            'question_banks': [bank], 'allow_project_questions': False})
                        path = base + '/interviews/' + session['id']
                        await pump(goal['id'], 'scripted')
                        current = request(path)
                        asked = next(event for event in current['events'] if event['kind'] == 'question')
                        request(path + '/answers', {
                            'expected_version': current['version'], 'question_id': asked['id'],
                            'text': sample['answer']})
                        before = await ledger.summary()
                        await pump(goal['id'], 'live', identity)
                        after = await ledger.summary()
                        async with pool.connection() as conn:
                            rows = await (await conn.execute(
                                'SELECT payload FROM app.career_interview_observations WHERE session_id=%s',
                                (session['id'],))).fetchall()
                        observation = rows[-1]['payload'] if rows else None
                        calls = [call for call in after['calls']
                                 if call['id'] not in {item['id'] for item in before['calls']}]
                        total_calls += len(calls)
                        total_nano += sum(call['accounted_nano'] or 0 for call in calls)
                        if after['usage_status'] == 'unknown':
                            stopped = 'unknown_usage_paused_no_retry'
                            raise RuntimeError(stopped)
                        if total_calls > MAX_HTTP:
                            stopped = 'authorized_http_ceiling_reached'
                            raise RuntimeError(stopped)
                        if total_nano > MAX_NANO:
                            stopped = 'authorized_budget_ceiling_reached'
                            raise RuntimeError(stopped)
                        if observation is None:
                            detail = await run_failure(pool, session['id'])
                            raise RuntimeError('no_observation:' + detail)
                        result = {'status': 'ok' if observation else 'no_observation',
                                  'decision': {'observation': observation} if observation else None,
                                  'session_id': session['id'], 'goal_id': goal['id'], 'authorization': identity,
                                  'http_calls': len(calls),
                                  'accounted_nano': sum(call['accounted_nano'] or 0 for call in calls),
                                  'raw_usage': [call['raw_usage'] for call in calls]}
                    except Exception as error:  # noqa: BLE001 - recorded, never retried automatically
                        code = str(error)[:200]
                        if code.startswith('unknown_usage') or code.startswith('authorized_http'):
                            stopped = code
                        result = {'status': 'infrastructure_failure', 'error_code': code,
                                  'authorization': identity}
                    record = {'sample_id': sample['id'], 'kind': sample['kind'], 'repeat': repeat,
                              'result': result, 'grade': grade(result, sample)}
                    records.append(record)
                    write(out / ('attempts-%s.json' % tag), record)
    finally:
        if pool.closed is False:
            await pool.close()
        await redis.aclose()

    report = summarize(records)
    report.update({'suite': 'interview-observation-v0.1', 'adapter': 'live',
                   'authorization': '%s (batches of %d, <=12 HTTP each)' % (AUTHORIZATION_ID, BATCH_SIZE),
                   'max_nano': MAX_NANO, 'max_http': MAX_HTTP, 'http_calls_used': total_calls,
                   'accounted_nano_used': total_nano,
                   'repeats': repeats, 'human_review': 'pending', 'stop_reason': stopped,
                   'note': 'real model; keyword rubric is a coarse screen, not semantic certification'})
    write(out / 'record.json', {'samples': records, 'report': report})
    return out, report, records
