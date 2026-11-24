"""F07Z-11: production material **generation probe** (NOT the frozen material judgement).

This runner does not grade the frozen `judgement-v0.5` material cases. It uploads each case's
sources, asks the production materials worker to generate a fresh rewrite, and then asks the
C1 guard whether that *generated* text contains a detectable violation. The frozen
`input.rewrite` (the candidate the gold actually judges) is never sent to the model and never
graded.

Two consequences that must stay in the report:
  * `grounding_expectation='pass'` is passed on purpose: the question is "did production
    produce something our deterministic guard would flag", not "does the guard catch this
    candidate". The gold's expected violation is therefore irrelevant here.
  * the frozen judgement baseline is a separate, free, offline run:
    `python -m interview_agent.evaluation.judgement run` (7 violation / 1 pass on v0.5.0).

Real path used here (no mocks):
  sign-up -> goal -> upload source -> POST /materials/rewrite -> live materials worker
  -> GET /materials -> draft rewrite_items -> C1 compare against the uploaded source.
"""
import argparse, asyncio, hashlib, json, secrets
from pathlib import Path
from uuid import uuid4

from .judgement import RESERVATION_NANO, c1_violations, grade, load_suite, run_cap_nano, write

AUTHORIZATION_PREFIX = 'f07x-04b-material'
# F07Z-10: same stage as the label rounds (0.45); this run gets whatever is left.
# F07Z-18: stage extended from 0.45 to 0.50 (user approved +0.05) for the 3-trial anchor
# regression the review recommended.
MAX_CNY = '0.5'
MAX_NANO = 500_000_000
# The ceiling must not trip before the per-authorization fuses do: `MaterialPolicy` allows 6
# HTTP per case, so the run-wide ceiling is 6 × cases (never a smaller arbitrary number).
MAX_HTTP_PER_CASE = 6
MAX_HTTP = 24
# F07Z-14: candidate comparison stage (baseline prompt vs grounding-v2), authorized 0.45.
RUN_BUDGET_NANO = 450_000_000


def credentials(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        name, _, value = line.partition('=')
        if name.strip() == 'DEEPSEEK_API_KEY' and value.strip():
            return value.strip()
    raise ValueError('deepseek_key_missing')


async def run(root, *, api_config, worker_config, owner_config, policy_path,
              credentials_path, output_dir, max_cases=None, run_budget_nano=None,
              prompt_variant='baseline', case_ids=None):
    budget_nano = RUN_BUDGET_NANO if run_budget_nano is None else run_budget_nano
    from .material_prompts import VARIANTS
    if prompt_variant not in VARIANTS:
        raise ValueError('unknown_prompt_variant')
    system_prompt = VARIANTS[prompt_variant]
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
    from interview_agent.materials.repository import MaterialRepository
    from interview_agent.materials.worker import worker_for
    from interview_agent.materials.billing import MaterialPolicy, MaterialLedger
    from interview_agent.materials.agent import SYSTEM_PROMPT

    api = Settings.load(api_config)
    worker = WorkerSettings.load(worker_config)
    owner = DatabaseSettings.model_validate_json(Path(owner_config).read_text(encoding='utf-8'))
    policy = MaterialPolicy.load(policy_path)
    if api.mode != 'development' or worker.mode != 'development' or api.database.dbname != 'py_interview':
        raise ValueError('live_run_requires_development_py_interview')
    if policy.max_http_attempts > 12:
        raise ValueError('policy_must_keep_the_fuse')
    key = credentials(credentials_path)

    manifest, cases = load_suite(root)
    selected = [case for case in cases if case.slice == 'material_grounding']
    if case_ids:
        unknown = set(case_ids) - {case.id for case in cases}
        if unknown:
            raise ValueError('unknown_case_ids:' + ','.join(sorted(unknown)))
        selected = [case for case in selected if case.id in set(case_ids)]
    if max_cases:
        selected = selected[:max_cases]
    http_ceiling = max(MAX_HTTP, MAX_HTTP_PER_CASE * len(selected))
    nonce = uuid4().hex
    out = Path(output_dir) / nonce
    out.mkdir(parents=True)
    write(out / 'plan.json', {'dataset': manifest['version'], 'slice': 'material_grounding',
                              'cases': [case.id for case in selected], 'budget_cny': MAX_CNY, 'prompt_variant': prompt_variant,
                              'run_budget_cny': round(budget_nano / 1e9, 6),
                              'max_http': MAX_HTTP, 'policy_profile': policy.profile})

    pool = AsyncConnectionPool(api.database.conninfo(), open=False, kwargs={'row_factory': dict_row})
    redis = worker.redis.connect()
    records, spent_http, spent_nano = [], 0, 0

    async def pump(goal_id, provider, authorization=None, stream_key=None):
        repo = MaterialRepository(pool)
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT * FROM app.career_material_runs WHERE goal_id=%s AND status='PENDING' "
                "ORDER BY created_at,id LIMIT 1", (goal_id,))).fetchone()
        if not row:
            raise RuntimeError('expected_pending_material_run')
        queue = QueueTransport(redis, stream_key)
        await queue.publish(QueueMessage(operation_id=str(row['id']), job_id=str(row['job_id']),
                                         resource_version=row['goal_version']))
        kwargs = {'provider': 'scripted'} if provider == 'scripted' else {
            'provider': 'live', 'policy': policy, 'authorization': authorization,
            'credentials': credentials_path, 'system_prompt': system_prompt}
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
                # Same aggregate guard as the interview runner (external review §17): the cap
                # is charged before a case starts, so the run cannot exceed the authorization.
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
                    async with await AsyncConnection.connect(owner.conninfo()) as conn:
                        if await (await conn.execute('SELECT id FROM app.model_authorizations WHERE id=%s',
                                                     (identity,))).fetchone():
                            raise RuntimeError('authorization_already_exists')
                    await authorize(owner, policy, identity, MAX_NANO)
                    ledger = MaterialLedger(pool, policy, identity)

                    source_text = '\n'.join(case.input.sources)
                    request('/api/auth/sign-up/email', {
                        'email': 'f07x-m-%s-%s@example.com' % (nonce, case.id.lower()),
                        'name': 'F07X synthetic', 'password': secrets.token_urlsafe(24)})
                    goal = request('/api/career-goals/', {
                        'message': 'F07X material baseline（合成，不研究岗位）', 'intent': 'training',
                        'jd_text': 'Java 后端：缓存改造、检索与报表模块；关注个人贡献与验证方法。',
                        'conditions': {'direction': 'Java 后端'}})
                    base = '/api/career-goals/' + goal['id']
                    uploaded = request(base + '/materials/sources', {
                        'expected_version': 1, 'expected_revision': 0, 'name': case.id + '.md',
                        'format': 'markdown', 'text': source_text})
                    listing = request(base + '/materials')
                    started = request(base + '/materials/rewrite', {
                        'expected_version': listing['goal_version'],
                        'expected_revision': listing['material_revision'],
                        'source_ids': [uploaded['source_id']]})
                    stream = 'py-interview:%s:%s:jobs:v1:materials' % (api.mode, nonce)
                    before = await ledger.summary()
                    await pump(goal['id'], 'live', authorization=identity, stream_key=stream)
                    after = await ledger.summary()
                    calls = [call for call in after['calls']
                             if call['id'] not in {item['id'] for item in before['calls']}]
                    spent_http += len(calls)
                    spent_nano += sum(call['accounted_nano'] or 0 for call in calls)
                    if after['usage_status'] == 'unknown':
                        raise RuntimeError('unknown_usage_paused_no_retry')
                    if spent_http > http_ceiling:
                        raise RuntimeError('authorized_http_ceiling_reached')

                    materials = request(base + '/materials')
                    drafts = materials.get('drafts') or []
                    draft = drafts[0] if drafts else {}
                    payload = draft.get('payload') or {}
                    items = payload.get('rewrite_items') or []
                    rewrite = ' '.join(item.get('text', '') for item in items).strip()
                    # The D decision moves all the "make it look better" value into
                    # suggestions, so they are part of the probe output, not a side note.
                    suggestions = list(payload.get('suggestions') or [])
                    if not rewrite:
                        raise RuntimeError('no_rewrite_items')
                    detected = c1_violations(rewrite, [source_text])
                    output = {'status': 'ok', 'decision': None, 'actions': [],
                              'rewrite': rewrite, 'source_text': source_text,
                              'suggestions': suggestions,
                              'detected_by_grader': detected,
                              'run_id': started.get('run_id')}
                    trial.update(output=output, http_calls=len(calls),
                                 accounted_nano=sum(call['accounted_nano'] or 0 for call in calls),
                                 draft_id=draft.get('id'))
                    trial['grade'] = grade(case, {'status': 'ok', 'decision': None,
                                                  'rewrite': rewrite, 'sources': [source_text]},
                                           grounding_expectation='pass')
                except Exception as error:  # noqa: BLE001 - recorded, never retried
                    trial.update(output={'status': 'protocol_failure', 'decision': None,
                                         'error_code': str(error)[:160]}, http_calls=0, accounted_nano=0)
                    trial['grade'] = grade(case, trial['output'])
                trial['case'] = case.model_dump(mode='json')
                records.append(trial)
                write(out / ('trial-%s.json' % case.id), trial)
    finally:
        if not pool.closed:
            await pool.close()
        await redis.aclose()

    graded = [r for r in records if r['grade']['status'] == 'graded']
    failures = [r for r in records if r['grade']['status'] != 'graded']
    report = {
        'dataset': manifest['version'], 'slice': 'material_grounding',
        'probe_kind': 'material_generation_probe',
        'task': 'production_generated_rewrite (NOT the frozen candidate rewrite)',
        'grades_frozen_gold': False,
        'repeat': 1,
        'scheduled_trials': len(records), 'valid_trials': len(graded),
        'protocol_failures': len(failures),
        'grounding': {'passed': sum(int(bool(r['grade']['passed'])) for r in graded),
                      'trials': len(graded),
                      'missed': [{'case': r['case_id'], 'missed': r['grade'].get('missed'),
                                  'unexpected': r['grade'].get('unexpected')} for r in graded
                                 if not r['grade']['passed']]},
        'cost': {'http_calls': spent_http, 'cny_upper': round(spent_nano / 1e9, 6)},
        'run_budget_cny': round(budget_nano / 1e9, 6),
        'not_run': [r['case_id'] for r in records if r['output'].get('status') == 'not_run'],
        # External review §11: a generation probe is only comparable across model/prompt
        # changes if it records what produced the text. No prompt version string exists, so
        # the digest is the version.
        'probe_metadata': {
            'source_case_ids': [r['case_id'] for r in records],
            'production_model': policy.model,
            'observed_model_version': getattr(policy, 'observed_model_version', None),
            'policy_profile': policy.profile,
            'prompt_source': 'interview_agent.materials.agent.SYSTEM_PROMPT',
            'prompt_variant': prompt_variant,
            'prompt_sha256': hashlib.sha256(system_prompt.encode('utf-8')).hexdigest(),
            'generated_rewrites': {r['case_id']: (r['output'] or {}).get('rewrite') for r in records},
            'c1_results': {r['case_id']: (r['output'] or {}).get('detected_by_grader') for r in records},
            'c2_review': 'pending',
        },
        'adapter': 'live', 'human_review': 'pending',
        'note': 'C1 is deterministic and only looks at the model-generated rewrite; '
                'C1 pass is not semantic grounding. The frozen material judgement is a '
                'separate offline run (judgement run).',
    }
    write(out / 'report.json', report)
    return out, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--api-config', type=Path, required=True)
    parser.add_argument('--worker-config', type=Path, required=True)
    parser.add_argument('--owner-config', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--max-cny', required=True)
    parser.add_argument('--prompt-variant', dest='prompt_variant', default='baseline',
                        choices=['baseline', 'legacy', 'grounding-v2', 'grounding-v3', 'grounding-d'],
                        help='candidate rewrite prompt; baseline is production')
    parser.add_argument('--run-budget', dest='run_budget',
                        help='CNY still left of the stage authorization for THIS run')
    parser.add_argument('--cases', nargs='+',
                        help='only these case ids (e.g. the failure-family anchors)')
    parser.add_argument('--max-cases', type=int)
    args = parser.parse_args(argv)
    if args.max_cny != MAX_CNY:
        parser.error('budget must equal the approved %s CNY stage' % MAX_CNY)
    try:
        budget_nano = run_cap_nano(MAX_CNY, args.run_budget or MAX_CNY)
    except ValueError as error:
        parser.error(str(error))
    import interview_agent.api.__main__ as api_main
    out, report = asyncio.run(run(
        args.root.resolve(), api_config=args.api_config, worker_config=args.worker_config,
        owner_config=args.owner_config, policy_path=args.policy,
        credentials_path=args.credentials,
        output_dir=args.root.resolve() / '.private/f07x/judgement-material',
        max_cases=args.max_cases, run_budget_nano=budget_nano,
        prompt_variant=args.prompt_variant, case_ids=args.cases),
        loop_factory=api_main.selector_loop)
    print(json.dumps({'status': 'recorded', 'output': str(out), 'metrics': report}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
