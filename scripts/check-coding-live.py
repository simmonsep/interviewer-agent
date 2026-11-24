"""JA-07-03 opt-in paid pilot. Never authorizes funds or reads credential contents."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import runpy
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.coding.billing import CodingLedger, CodingPolicy, AUTHORIZATION_ID
from interview_agent.execution.docker_java import DockerJava, DockerProfile
from interview_agent.storage.config import DatabaseSettings, Settings
from interview_agent.worker.config import WorkerSettings

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:3100'


class PilotFailure(RuntimeError):
    pass


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def request(client, path, body=None, key=None):
    response = client.get(path) if body is None else client.post(path, json=body, headers={'Idempotency-Key': key or uuid4().hex})
    if response.status_code >= 400:
        try: code = response.json().get('code', 'request_failed')
        except ValueError: code = 'request_failed'
        if not isinstance(code, str) or not re.fullmatch(r'[a-z0-9_]{1,100}', code): code = 'request_failed'
        raise PilotFailure(f'http_{response.status_code}_{code}')
    return response.json()


def binding(session):
    return {'expected_version': session['version'], 'expected_workspace_sha256': session['workspace']['sha256']}


def wait(client, path, artifact, run_id, timeout=260):
    """Poll one saved run. Failure/timeout is evidence, never an automatic paid retry."""
    until = time.monotonic() + timeout; latest = None
    while time.monotonic() < until:
        latest = request(client, path)
        row = next((r for r in latest['runs'] if r['id'] == run_id), None)
        if row and row['status'] not in ('PENDING', 'RUNNING'):
            write(artifact, latest)
            if row['status'] != 'SUCCEEDED': raise PilotFailure('coding_run_failed_' + row['kind'])
            return latest
        time.sleep(.3)
    if latest is not None: write(artifact, latest)
    raise PilotFailure('coding_run_timeout')


async def audit(owner, policy, *, require_active=False):
    """Read-only account checks. Old UNKNOWN rows belong to their original authorizations."""
    async with await AsyncConnection.connect(owner.conninfo(), row_factory=dict_row) as conn:
        auth = await (await conn.execute('SELECT id,policy_hash,max_nano,state FROM app.model_authorizations WHERE id=%s', (AUTHORIZATION_ID,))).fetchone()
        legacy = await (await conn.execute('''SELECT id::text,authorization_id,state,reserved_nano,accounted_nano
            FROM app.model_http_calls WHERE authorization_id<>%s ORDER BY id''', (AUTHORIZATION_ID,))).fetchall()
        applied = await (await conn.execute('SELECT sha256 FROM infra.schema_migrations WHERE name=%s', ('014_coding_live.sql',))).fetchone()
    if not auth or auth['policy_hash'] != policy.digest: raise PilotFailure('coding_authorization_missing_or_policy_mismatch')
    if not 0 < auth['max_nano'] <= 2_000_000_000: raise PilotFailure('coding_authorization_outside_cny2')
    if not applied or applied['sha256'] != hashlib.sha256((ROOT / 'backend/migrations/014_coding_live.sql').read_bytes()).hexdigest():
        raise PilotFailure('coding_live_migration_not_applied')
    pool = AsyncConnectionPool(owner.conninfo(), open=False, kwargs={'row_factory': dict_row})
    try:
        await pool.open(wait=True)
        summary = await CodingLedger(pool, policy, AUTHORIZATION_ID).summary()
    finally: await pool.close()
    if require_active:
        if auth['state'] != 'ACTIVE' or summary['usage_status'] not in ('known', 'not_called'):
            raise PilotFailure('coding_authorization_not_ready')
        if (summary['http_attempts'] >= policy.max_http_attempts
                or summary['accounted_upper_nano'] + policy.reservation_nano > auth['max_nano']):
            raise PilotFailure('coding_authorization_budget_exhausted')
    return {'authorization': auth, 'summary': summary, 'legacy_model_calls': legacy}


def scenario(client, identities, out, receipt):
    for identity in identities:
        record = {'task_id': identity, 'status': 'started', 'final_grade_run': False}
        receipt['tasks'].append(record)
        goal = request(client, '/api/career-goals/', {'message': 'JA-07-03合成真实模型练习，无简历。', 'conditions': {'direction': 'Java后端'}})
        record['goal_id'] = goal['id']
        session = request(client, f"/api/career-goals/{goal['id']}/coding", {'expected_version': goal['version'], 'task_id': identity, 'mode': 'practice'})
        record['session_id'] = session['id']; path = f"/api/career-goals/{goal['id']}/coding/{session['id']}"
        write(out / (identity + '-created.json'), session)
        if session['policy'].get('assistant_provider') != 'live': raise PilotFailure('session_live_policy_missing')
        initial = session['workspace']
        public = request(client, path + '/public-tests', binding(session))
        session = wait(client, path, out / (identity + '-original.json'), public['run_id'])
        if session['runs'][-1]['result']['grade']['verdict'] != 'WRONG_ANSWER': raise PilotFailure('starter_failure_not_observed')
        # This is the only paid command for this task; never resubmit it on failure.
        assist = request(client, path + '/assist', binding(session))
        record['assist_run_id'] = assist['run_id']
        session = wait(client, path, out / (identity + '-proposal.json'), assist['run_id'])
        proposal = next(r for r in session['runs'] if r['id'] == assist['run_id'])
        if proposal['origin'] != 'real_model' or session['workspace'] != initial: raise PilotFailure('proposal_origin_or_user_workspace_changed')
        candidate = proposal['result']['proposal']; trace = candidate['tool_log']
        record.update(model_requests=proposal['model_requests'], assistant_responses=proposal['assistant_responses'],
                      public_executions=proposal['executions'], tool_log=trace)
        if candidate['status'] != 'verified': raise PilotFailure('live_candidate_not_verified')
        if not candidate['snapshot']['edits']: raise PilotFailure('live_candidate_has_no_edit')
        verdicts = [test.get('result', {}).get('verdict') for test in candidate['tests']]
        if verdicts[0:1] != ['WRONG_ANSWER'] or verdicts[-1:] != ['ACCEPTED']:
            raise PilotFailure('live_baseline_failure_and_recovery_not_observed')
        successful = {x['tool'] for x in trace if x['status'] == 'ok'}
        if not {'read_file', 'replace_file', 'run_public_tests'} <= successful: raise PilotFailure('live_tool_evidence_incomplete')
        decision = 'accept' if identity == 'ttl-boundary' else 'reject'
        explanation = '合成用户检查补丁后接受；代码贡献仍归助手。' if decision == 'accept' else '合成用户拒绝助手提议，保留自己的原始代码，后续自行研究。'
        session = request(client, path + '/decisions', {**binding(session), 'run_id': proposal['id'], 'decision': decision, 'explanation': explanation})
        write(out / (identity + '-decision.json'), session)
        if session['commands'][-1]['input_payload']['explanation'] != explanation: raise PilotFailure('user_explanation_not_preserved')
        record['decision'] = decision
        if decision == 'accept':
            if session['versions'][-1]['origin'] != 'assistant' or session['workspace'] == initial:
                raise PilotFailure('assistant_acceptance_not_applied')
            submitted = request(client, path + '/submit', {**binding(session), 'explanation': '合成用户提交接受的助手代码；通过只说明固定用例，不证明独立编程。'})
            session = wait(client, path, out / (identity + '-final.json'), submitted['run_id'])
            final = next(r for r in session['runs'] if r['id'] == submitted['run_id'])
            if session['status'] != 'COMPLETED' or final['result']['grade']['verdict'] != 'ACCEPTED': raise PilotFailure('live_final_grade_not_accepted')
            if len(final['result']['grade']['cases']) != 4: raise PilotFailure('live_final_case_coverage_incomplete')
            record.update(final_grade_run=True, final_verdict='ACCEPTED', outcome='accepted_assistant_code_passed_fixed_tests')
        else:
            if session['workspace'] != initial or len(session['versions']) != 1: raise PilotFailure('rejected_proposal_changed_user_code')
            record.update(final_verdict=None, outcome='proposal_rejected_original_code_preserved_no_final_grade')
        if request(client, path)['workspace'] != session['workspace']: raise PilotFailure('refresh_did_not_preserve_workspace')
        record.update(status='passed', independent_ability_certified=False)
        write(out / (identity + '-result.json'), record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true', help='Explicitly execute the already authorized paid pilot; does not authorize funds')
    parser.add_argument('--credentials', type=Path, required=True, help='Explicit private file passed to Worker; never read by this script')
    parser.add_argument('--policy', type=Path, default=ROOT / 'configs/ja07-deepseek-flash.json')
    parser.add_argument('--profile', type=Path, default=ROOT / 'configs/ja06-docker-java.json')
    parser.add_argument('--task', choices=['all', 'ttl-boundary', 'order-cancel'], default='all')
    args = parser.parse_args()
    if not args.execute_live: parser.error('--execute-live is required; no model request was made')
    if not args.credentials.is_file(): parser.error('explicit credentials file does not exist')
    with socket.socket() as sock: sock.bind(('127.0.0.1', 3100))
    out = ROOT / '.private/ja-07-03/live' / uuid4().hex; out.mkdir(parents=True)
    processes = []; handles = []; error = None; before = None; owner = None; policy = None; profile = None
    receipt = {'status': 'failed', 'authorization_id': AUTHORIZATION_ID, 'tasks': [], 'paid_retry_performed': False,
               'credentials_handling': 'Explicit path passed to Worker; script never reads key contents',
               'pilot_records_retained': True, 'scope': 'Small controlled synthetic tasks; not a benchmark or independent human ability assessment'}
    try:
        profile = DockerProfile.load(args.profile)
        isolation = asyncio.run(runpy.run_path(str(ROOT / 'scripts/check-executor.py'))['check'](profile))
        write(out / 'isolation.json', isolation)
        policy = CodingPolicy.load(args.policy)
        owner = DatabaseSettings.model_validate_json((ROOT / '.private/postgres/migration.json').read_text(encoding='utf-8'))
        app = Settings.load(ROOT / '.private/postgres/app.json')
        worker = WorkerSettings.load(ROOT / '.private/redis/worker.json')
        if (owner.dbname != 'py_interview' or owner.user != 'py_interview_owner' or app.database.dbname != 'py_interview'
                or app.mode != 'development' or worker.database.dbname != 'py_interview' or worker.mode != 'development'):
            raise PilotFailure('live_pilot_requires_development_database')
        before = asyncio.run(audit(owner, policy, require_active=True), loop_factory=selector_loop)
        write(out / 'billing-before.json', before)
        commands = [('api', [sys.executable, '-m', 'interview_agent.api', '--config', '.private/postgres/app.json', '--coding-executor-profile', str(args.profile), '--coding-assistant-mode', 'live']),
                    ('coding-worker', [sys.executable, '-m', 'interview_agent.coding.worker', '--config', '.private/redis/worker.json', '--executor', 'docker', '--profile', str(args.profile), '--provider', 'live', '--policy', str(args.policy), '--authorization', AUTHORIZATION_ID, '--credentials', str(args.credentials.resolve())])]
        for name, command in commands:
            handle = (out / (name + '.log')).open('wb'); handles.append(handle)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0))
        with httpx.Client(trust_env=False, timeout=1) as probe:
            until = time.monotonic() + 20
            while True:
                if any(p.poll() is not None for p in processes): raise PilotFailure('owned_service_exited')
                try:
                    if probe.get(BASE + '/health').status_code == 200: break
                except httpx.HTTPError: pass
                if time.monotonic() > until: raise PilotFailure('owned_API_startup_timeout')
                time.sleep(.2)
        with httpx.Client(base_url=BASE, headers={'Origin': BASE}, trust_env=False, timeout=10) as client:
            email = 'ja0703-' + out.name + '@example.com'
            signed = request(client, '/api/auth/sign-up/email', {'name': 'JA0703 Synthetic', 'email': email, 'password': secrets.token_urlsafe(24)})
            receipt.update(synthetic_user_id=signed['user']['id'], synthetic_email=email)
            identities = ['ttl-boundary', 'order-cancel'] if args.task == 'all' else [args.task]
            scenario(client, identities, out, receipt)
        receipt['status'] = 'passed'
    except Exception as exc:
        error = str(exc) if isinstance(exc, PilotFailure) else getattr(exc, 'code', type(exc).__name__)
        if not isinstance(error, str) or len(error) > 200: error = type(exc).__name__
        receipt['error'] = error
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        for handle in handles: handle.close()
        receipt['owned_processes_stopped'] = all(p.poll() is not None for p in processes)
        # Never delete pilot accounts/goals or billing rows: preserve their association.
        if owner is not None and policy is not None:
            try:
                after = asyncio.run(audit(owner, policy), loop_factory=selector_loop)
                write(out / 'billing-after.json', after)
                receipt['authorization_total'] = after['summary']
                if before is not None:
                    receipt['pilot_http_attempts'] = after['summary']['http_attempts'] - before['summary']['http_attempts']
                    receipt['pilot_accounted_upper_delta_nano'] = after['summary']['accounted_upper_nano'] - before['summary']['accounted_upper_nano']
                    receipt['legacy_model_calls_unchanged'] = after['legacy_model_calls'] == before['legacy_model_calls']
                    if not receipt['legacy_model_calls_unchanged']: receipt['status'] = 'failed'; receipt['error'] = 'legacy_model_calls_changed'
                if after['summary']['usage_status'] == 'unknown': receipt['status'] = 'failed'; receipt['error'] = 'model_usage_unknown_reservations_retained'
                if after['summary']['accounted_upper_nano'] > after['authorization']['max_nano']:
                    receipt['status'] = 'failed'; receipt['error'] = 'authorization_bound_exceeded'
            except Exception as exc:
                receipt['status'] = 'failed'; receipt['billing_finalization_error'] = type(exc).__name__
        if profile is not None:
            try:
                receipt['remaining_candidate_containers'] = len(asyncio.run(DockerJava(profile).must(['ps', '-aq', '--filter', 'label=interview-agent.executor=ja06'])).split())
                if receipt['remaining_candidate_containers']: receipt['status'] = 'failed'; receipt['error'] = 'candidate_containers_remain'
            except Exception as exc:
                receipt['status'] = 'failed'; receipt['container_finalization_error'] = type(exc).__name__
        for record in receipt['tasks']:
            if record['status'] != 'passed':
                record.update(status='failed', error=receipt.get('error', 'pilot_incomplete'))
            write(out / (record['task_id'] + '-result.json'), record)
        write(out / 'receipt.json', receipt)
    print(json.dumps({'status': receipt['status'], 'receipt': str(out / 'receipt.json'), 'error': receipt.get('error', error)}, ensure_ascii=False))
    return 0 if receipt['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
