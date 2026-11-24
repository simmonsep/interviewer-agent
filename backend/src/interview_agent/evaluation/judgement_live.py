"""F07X-04: live baseline for the judgement benchmark.

Same real path as F07U: authenticated ASGI API -> real interview worker -> real ledger.
Each case gets its own bounded authorization (LivePolicy caps one at 12 HTTP), and the
aggregate ceiling is enforced by counters here, not by raising the fuse.

Deliberately NOT wired in this file
  * material_grounding: the production rewrite lives in the *materials* worker with its own
    policy/credentials. Scoring C1 needs that worker's output, so it is a separate step
    (F07X-04b). It is reported as `not_run`, never as a pass.
  * anything that would require a tool the model does not have.

Usage
  python -m interview_agent.evaluation.judgement_live --slices observation abstention
  python -m interview_agent.evaluation.judgement_live --slices injection
"""
import argparse, asyncio, json, secrets
from pathlib import Path
from uuid import uuid4

from .judgement import (c1_violations, derive_actions, grade, label_diagnostics, load_suite,
                        run_cap_nano, write)

AUTHORIZATION_PREFIX = 'f07z-05-live'
# F07Z-08: the user authorized CNY 0.45 for this stage (was 0.30 in F07Z-07).
MAX_CNY = '0.45'
# Per-case authorization. It must stay above `policy.reservation_nano` (0.147456 CNY),
# otherwise the ledger refuses the very first request of a case.
MAX_NANO = 200_000_000
MAX_HTTP = 64
# The aggregate guard is a *worst case* guard, not an estimate: a case is only started if
# `spent_actual + one full reservation` still fits, so no single request can push the run
# over the authorized total even if every request settled at the reservation ceiling.
# Consequence, stated on purpose: one reservation (0.147456) is held back from every run,
# so a run's usable actual spend is `cap - reservation`.
RUN_BUDGET_NANO = 450_000_000
SLICE_BUDGET = {'observation': 20, 'abstention': 20, 'injection': 24}


def select_cases(cases, slices, options, max_cases=None):
    """Pure selector. Takes the options **dict** on purpose: reading it with getattr used to
    match nothing, silently running every case and overspending a paid batch."""
    selected = [case for case in cases if case.slice in slices]
    wanted = (options or {}).get('cases')
    if wanted:
        lookup = {case.id for case in cases}
        unknown = set(wanted) - lookup
        if unknown:
            raise ValueError('unknown_case_ids:' + ','.join(sorted(unknown)))
        selected = [case for case in selected if case.id in set(wanted)]
    if max_cases:
        selected = selected[:max_cases]
    return selected


def bank_item(case, slice_name):
    # NOTE: never substitute the question for a missing reference. The product derives
    # `has_reference = bool(reference)`, so filling it in silently turns the no-reference
    # contract test (ABS-002) into an ordinary reference case.
    item = {'item_id': case.id, 'kind': 'knowledge', 'roles': ['java-backend'],
            'title': case.id, 'question': case.input.question,
            'reference': case.input.reference,
            'source_url': 'https://example.invalid/judgement-v0.3.0',
            'source_note': 'judgement-v0.3.0 synthetic case; not a company question.',
            'tags': ['judgement', slice_name]}
    if slice_name == 'injection':
        # a neutral legal follow-up, so "finish now" is a *choice* rather than the only option
        item['probe'] = '请再具体说明你如何验证这一点。'
    return {'schema_version': 1, 'name': 'F07X ' + case.id, 'items': [item]}


def credentials(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        name, _, value = line.partition('=')
        if name.strip() == 'DEEPSEEK_API_KEY' and value.strip():
            return value.strip()
    raise ValueError('deepseek_key_missing')


async def run(root, slices, *, api_config, worker_config, owner_config, policy_path,
              credentials_path, output_dir, max_cases=None, options=None, run_budget_nano=None):
    budget_nano = RUN_BUDGET_NANO if run_budget_nano is None else run_budget_nano
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

    api = Settings.load(api_config)
    worker = WorkerSettings.load(worker_config)
    owner = DatabaseSettings.model_validate_json(Path(owner_config).read_text(encoding='utf-8'))
    policy = InterviewPolicy.load(policy_path)
    if api.mode != 'development' or worker.mode != 'development' or api.database.dbname != 'py_interview':
        raise ValueError('live_run_requires_development_py_interview')
    if policy.max_http_attempts > 12:
        raise ValueError('policy_must_keep_the_12_http_fuse')
    key = credentials(credentials_path)

    manifest, cases = load_suite(root)
    selected = select_cases(cases, slices, options, max_cases)
    if not selected:
        raise ValueError('no_cases_selected')

    nonce = uuid4().hex
    out = Path(output_dir) / nonce
    out.mkdir(parents=True)
    write(out / 'plan.json', {'dataset': manifest['version'], 'slices': list(slices),
                              'cases': [case.id for case in selected],
                              'budget_cny': MAX_CNY, 'max_http': MAX_HTTP,
                              'run_budget_cny': round(budget_nano / 1e9, 6),
                              'per_slice_http_budget': {k: SLICE_BUDGET[k] for k in slices},
                              'deterministic_label_branch': [case.id for case in selected
                                                       if not case.input.policy.has_reference],
                              'not_wired': ['material_grounding (materials worker, F07X-04b)']})

    pool = AsyncConnectionPool(api.database.conninfo(), open=False, kwargs={'row_factory': dict_row})
    redis = worker.redis.connect()
    records, spent_http, spent_nano = [], 0, 0

    async def pump(goal_id, provider, authorization=None, stream_key=None):
        repo = InterviewRepository(pool)
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT * FROM app.career_interview_runs WHERE goal_id=%s AND status='PENDING' "
                "ORDER BY created_at,id LIMIT 1", (goal_id,))).fetchone()
        if not row:
            raise RuntimeError('expected_pending_interview_run')
        queue = QueueTransport(redis, stream_key)
        await queue.publish(QueueMessage(operation_id=str(row['id']), job_id=str(row['job_id']),
                                         resource_version=row['goal_version']))
        kwargs = {'provider': 'scripted'} if provider == 'scripted' else {
            'provider': 'live', 'policy': policy, 'authorization': authorization,
            'credentials': credentials_path}
        if not await worker_for(repo, queue, **kwargs).once():
            raise RuntimeError('worker_did_not_take_owned_job')

    await pool.open(wait=True)
    await redis.ping()
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

            for index, case in enumerate(selected, start=1):
                identity = '%s-%s-c%02d' % (AUTHORIZATION_PREFIX, nonce[:6], index)
                trial = {'case_id': case.id, 'slice': case.slice, 'authorization': identity}
                # A+ (v0.5 review §5 / Required 2): a case without a reference is a
                # deterministic product branch, not a model judgement. It still goes through
                # the real API / worker / DB path, but with the scripted provider: 0 model
                # HTTP calls, 0 cost, and it stays out of the model quality denominator.
                deterministic = not case.input.policy.has_reference
                trial['deterministic_label_branch'] = deterministic
                if spent_nano + int(policy.reservation_nano) > budget_nano or spent_http >= MAX_HTTP:
                    trial.update(output={'status': 'not_run', 'error_code': 'run_budget_cap_reached'},
                                 http_calls=0, accounted_nano=0,
                                 grade={'status': 'not_graded', 'passed': False,
                                        'error_code': 'run_budget_cap_reached'})
                    trial['case'] = case.model_dump(mode='json')
                    records.append(trial)
                    write(out / ('trial-%s.json' % case.id), trial)
                    continue
                try:
                    if deterministic:
                        ledger = None
                    else:
                        async with await AsyncConnection.connect(owner.conninfo()) as conn:
                            if await (await conn.execute('SELECT id FROM app.model_authorizations WHERE id=%s',
                                                         (identity,))).fetchone():
                                raise RuntimeError('authorization_already_exists')
                        await authorize(owner, policy, identity, MAX_NANO)
                        ledger = InterviewLedger(pool, policy, identity)

                    request('/api/auth/sign-up/email', {
                        'email': 'f07x-%s-%s@example.com' % (nonce, case.id.lower()),
                        'name': 'F07X synthetic', 'password': secrets.token_urlsafe(24)})
                    goal = request('/api/career-goals/', {
                        'message': 'F07X judgement baseline（合成，不研究岗位）', 'intent': 'training',
                        'conditions': {'direction': 'Java 后端'}})
                    base = '/api/career-goals/' + goal['id']
                    preview = request('/api/question-banks/imports', {'document': bank_item(case, case.slice)})
                    bank = request('/api/question-banks/imports/%s/commit' % preview['preview_id'], {})
                    max_questions = 2 if case.slice == 'injection' else 1
                    session = request(base + '/interviews', {
                        'expected_version': 1, 'role': 'java-backend', 'mode': 'interview',
                        'max_questions': max_questions, 'duration_seconds': 3600,
                        'question_banks': [bank], 'allow_project_questions': False})
                    path = base + '/interviews/' + session['id']
                    stream = 'py-interview:%s:%s:jobs:v1:interviews' % (api.mode, nonce)

                    await pump(goal['id'], 'scripted', stream_key=stream)
                    current = request(path)
                    asked = next(event for event in current['events'] if event['kind'] == 'question')
                    request(path + '/answers', {'expected_version': current['version'],
                                                'question_id': asked['id'], 'text': case.input.answer})
                    before = await ledger.summary() if ledger else {'calls': []}
                    await pump(goal['id'], 'scripted' if deterministic else 'live',
                               authorization=identity, stream_key=stream)
                    after = await ledger.summary() if ledger else {'calls': [], 'usage_status': 'not_applicable'}
                    async with pool.connection() as conn:
                        rows = await (await conn.execute(
                            'SELECT payload FROM app.career_interview_observations WHERE session_id=%s',
                            (session['id'],))).fetchall()
                        run_row = await (await conn.execute(
                            "SELECT status,error_code,result FROM app.career_interview_runs "
                            "WHERE session_id=%s ORDER BY created_at DESC LIMIT 1", (session['id'],))).fetchone()
                    calls = [call for call in after['calls']
                             if call['id'] not in {item['id'] for item in before['calls']}]
                    spent_http += len(calls)
                    spent_nano += sum(call['accounted_nano'] or 0 for call in calls)
                    if after['usage_status'] == 'unknown':
                        raise RuntimeError('unknown_usage_paused_no_retry')
                    if spent_http > MAX_HTTP:
                        raise RuntimeError('authorized_http_ceiling_reached')
                    observation = rows[-1]['payload'] if rows else None
                    decision = {'observation': observation} if observation else None
                    tool_log = ((run_row or {}).get('result') or {}).get('tool_log') or []
                    remaining = max_questions - 1
                    actions = derive_actions(decision, remaining, tool_log)
                    output = {'status': 'ok' if observation else 'protocol_failure',
                              'decision': decision, 'actions': actions,
                              'remaining_questions': remaining, 'tool_log': tool_log,
                              'error_code': None if observation else ((run_row or {}).get('error_code') or 'no_observation')}
                    if deterministic:
                        # The anchor's claim is "the product derives this without the model".
                        # A live call here would make that claim false, so it is a hard failure.
                        label = (observation or {}).get('label')
                        output['deterministic_label_contract'] = (
                            'ok' if label == 'inconclusive' and not calls else
                            'violated:label=%s,calls=%s' % (label, len(calls)))
                    trial.update(output=output, http_calls=len(calls),
                                 accounted_nano=sum(call['accounted_nano'] or 0 for call in calls),
                                 model_requests=0 if deterministic else len(calls))
                except Exception as error:  # noqa: BLE001 - recorded, never retried
                    trial.update(output={'status': 'protocol_failure', 'decision': None,
                                         'error_code': str(error)[:160]}, http_calls=0,
                                 accounted_nano=0, model_requests=0)
                trial['grade'] = grade(case, trial['output'])
                trial['case'] = case.model_dump(mode='json')
                records.append(trial)
                write(out / ('trial-%s.json' % case.id), trial)
    finally:
        if not pool.closed:
            await pool.close()
        await redis.aclose()

    report = summarize_live(records, spent_http, spent_nano, manifest, slices)
    report['run_budget_cny'] = round(budget_nano / 1e9, 6)
    write(out / 'report.json', report)
    return out, report


def summarize_live(records, spent_http, spent_nano, manifest, slices):
    graded = [r for r in records if r['grade']['status'] == 'graded']
    failures = [r for r in records if r['grade']['status'] != 'graded']
    # A+ (v0.5 review §5): has_reference=false is a deterministic product branch, so it is
    # counted separately and never inflates the model quality denominator.
    quality = [r for r in graded if r['slice'] in ('observation', 'abstention')
               and not r.get('deterministic_label_branch')]
    deterministic = [r for r in graded if r['slice'] in ('observation', 'abstention')
                     and r.get('deterministic_label_branch')]
    injection = [r for r in graded if r['slice'] == 'injection']
    label_ok = sum(int(bool(r['grade']['label_correct'])) for r in quality)
    return {
        'dataset': manifest['version'],
        'slices_run': list(slices),
        'not_run': [s for s in ('observation', 'abstention', 'material_grounding', 'injection') if s not in slices],
        'scheduled_trials': len(records),
        'valid_trials': len(graded),
        'protocol_failures': len(failures),
        'quality': {'trials': len(quality), 'label_correct': label_ok,
                    'label_accuracy': round(label_ok / len(quality), 4) if quality else None,
                    'rationale_miss': sum(int(r['grade']['rationale_guard'] == 'miss') for r in quality),
                    'flag_for_human_review': sum(int(bool(r['grade'].get('flag_for_human_review'))) for r in quality)},
        'deterministic_label_branch': {
            'trials': len(deterministic),
            'label_correct': sum(int(bool(r['grade']['label_correct'])) for r in deterministic),
            # Two different facts, deliberately not merged (external review §6):
            # what the benchmark spent to validate the contract, versus what production does.
            'provider_mode': 'scripted',
            'live_provider_requests': sum(int(r.get('model_requests') or 0) for r in deterministic),
            'benchmark_provider_cost_cny': round(sum(int(r.get('accounted_nano') or 0) for r in deterministic) / 1e9, 6),
            'contract': [{'case': r['case_id'], 'result': (r['output'] or {}).get('deterministic_label_contract')}
                         for r in deterministic],
            'proves': 'has_reference=false -> effective label inconclusive, through real API/worker/DB',
            'does_not_prove': 'that production spends no model call on a no-reference question',
        },
        'production_behavior': {
            'label_source': 'deterministic',
            'action_source': 'model',
            'action_planning_may_call_model': True,
            'note': 'A+ fixes the label only; ask_question/finish/topic choice is still model-driven',
        },
        'label_diagnostics': label_diagnostics(records),
        'injection': {'trials': len(injection),
                      'followed': sum(int(bool(r['grade']['forbidden_conclusion'])) for r in injection)},
        'reliability': {'protocol_success_rate': round(1 - len(failures) / len(records), 4) if records else None,
                        'failures': [{'case': r['case_id'], 'error': r['output'].get('error_code')} for r in failures]},
        'cost': {'http_calls': spent_http, 'cny_upper': round(spent_nano / 1e9, 6),
                 'model_trials': len(quality) + len(injection),
                 'cost_per_valid_trial': round(spent_nano / 1e9 / len(graded), 6) if graded else None},
        'adapter': 'live',
        'semantic_validity': 'not_established',
        'human_review': 'pending',
        'note': 'material_grounding not wired in this run (materials worker, F07X-04b)',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--slices', nargs='+', default=['observation', 'abstention'],
                        choices=['observation', 'abstention', 'injection'])
    parser.add_argument('--api-config', type=Path, required=True)
    parser.add_argument('--worker-config', type=Path, required=True)
    parser.add_argument('--owner-config', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--max-cny', required=True)
    parser.add_argument('--run-budget', dest='run_budget',
                        help='CNY still left of the stage authorization for THIS run; '
                             'defaults to the whole stage')
    parser.add_argument('--max-cases', type=int)
    parser.add_argument('--cases', nargs='+')
    args = parser.parse_args(argv)
    if args.max_cny != MAX_CNY:
        parser.error('budget must equal the approved %s CNY stage' % MAX_CNY)
    try:
        budget_nano = run_cap_nano(MAX_CNY, args.run_budget or MAX_CNY)
    except ValueError as error:
        parser.error(str(error))
    root = args.root.resolve()
    import interview_agent.api.__main__ as api_main
    out, report = asyncio.run(run(
        root, args.slices, api_config=args.api_config, worker_config=args.worker_config,
        owner_config=args.owner_config, policy_path=args.policy,
        credentials_path=args.credentials,
        output_dir=root / '.private/f07x/judgement-live', max_cases=args.max_cases,
        options={'cases': args.cases}, run_budget_nano=budget_nano),
        loop_factory=api_main.selector_loop)
    print(json.dumps({'status': 'recorded', 'output': str(out), 'metrics': report}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
