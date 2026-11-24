"""JA-07-02: actual auth HTTP/SQL/Redis/independent Worker/Docker; scripted, no model Key."""
import argparse
import asyncio
import json
from pathlib import Path
import runpy
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4
import httpx
from psycopg import AsyncConnection
from interview_agent.api.__main__ import selector_loop
from interview_agent.coding.workspace import file_hash
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate
from interview_agent.execution.docker_java import DockerJava, DockerProfile

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:3100'


def write(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def request(c, path, body=None, key=None):
    r = c.get(path) if body is None else c.post(path, json=body, headers={'Idempotency-Key': key or uuid4().hex})
    if r.status_code >= 400: raise RuntimeError(f'{path}: {r.status_code}/{r.json().get("code")}')
    return r.json()


def binding(s): return {'expected_version': s['version'], 'expected_workspace_sha256': s['workspace']['sha256']}


def wait(c, path):
    until = time.monotonic() + 150
    while time.monotonic() < until:
        s = request(c, path)
        if s['runs'] and s['runs'][-1]['status'] not in ('PENDING', 'RUNNING'):
            if s['runs'][-1]['status'] != 'SUCCEEDED': raise RuntimeError(str(s['runs'][-1]))
            return s
        time.sleep(.25)
    raise RuntimeError('coding_worker_timeout')


def scenario(email, out, profile):
    checks = []; sessions = []
    with httpx.Client(base_url=BASE, headers={'Origin': BASE}, trust_env=False, timeout=10) as c:
        request(c, '/api/auth/sign-up/email', {'name': 'JA0702 Synthetic', 'email': email, 'password': secrets.token_urlsafe(24)})
        for identity in ('ttl-boundary', 'order-cancel'):
            g = request(c, '/api/career-goals/', {'message': '无简历，Java后端AI Coding练习', 'conditions': {'direction': 'Java后端'}})
            base = f"/api/career-goals/{g['id']}"; key = uuid4().hex; body = {'expected_version': 1, 'task_id': identity, 'mode': 'practice'}
            s = request(c, base + '/coding', body, key); replay = request(c, base + '/coding', body, key)
            assert s['id'] == replay['id']; sessions.append(s['id']); path = base + '/coding/' + s['id']
            initial = s['workspace']; assert 'zero-ttl' not in str(s) and 'cancel-repeat' not in str(s)
            request(c, path + '/public-tests', binding(s)); s = wait(c, path)
            assert s['runs'][-1]['result']['grade']['verdict'] == 'WRONG_ANSWER'
            write(out / (identity + '-original.json'), s)
            key = uuid4().hex; body = binding(s)
            a = request(c, path + '/assist', body, key); b = request(c, path + '/assist', body, key)
            assert a['run_id'] == b['run_id']
            s = wait(c, path); proposal = s['runs'][-1]; assert s['workspace'] == initial
            assert proposal['assistant_responses'] == 7 and proposal['origin'] == 'scripted_protocol'
            assert len(proposal['result']['proposal']['tests']) == 2
            assert 'zero-ttl' not in str(s) and 'cancel-repeat' not in str(s)
            write(out / (identity + '-proposal.json'), s)
            s = request(c, path + '/decisions', {**binding(s), 'run_id': proposal['id'], 'decision': 'accept', 'explanation': '我已检查补丁，接受助手提议。'})
            assert len(s['versions']) == 2 and s['versions'][-1]['origin'] == 'assistant'
            # A real user-origin version remains separate from the assistant version.
            filename = s['task']['editable'][0]; code = s['workspace']['files'][filename]
            body = {**binding(s), 'path': filename, 'expected_sha256': file_hash(code), 'text': code + '\n// User reviewed boundary conditions.\n', 'explanation': '我保留补丁，并记录自己的代码审查。'}
            key = uuid4().hex; s = request(c, path + '/edits', body, key)
            assert request(c, path + '/edits', body, key)['workspace'] == s['workspace']
            assert [v['origin'] for v in s['versions']] == ['starter', 'assistant', 'user']
            s = request(c, path + '/submit', {**binding(s), 'explanation': '边界与原有分支均需验证；提交当前代码版本。'})['session']
            assert s['status'] == 'SUBMITTED'
            s = wait(c, path); write(out / (identity + '-final.json'), s)
            assert s['status'] == 'COMPLETED' and s['runs'][-1]['result']['grade']['verdict'] == 'ACCEPTED'
            assert len(s['runs'][-1]['result']['grade']['cases']) == 4
            assert request(c, path)['versions'] == s['versions'] and not s['ability_certified']
            checks.extend([identity + ':creation_and_request_replay', identity + ':actual_starter_failure',
                           identity + ':proposal_does_not_mutate_user_code', identity + ':explicit_accept_and_user_version',
                           identity + ':public_hidden_separation', identity + ':frozen_final_actual_tests_and_refresh'])
        # Observe actual candidate Java before cancelling the SQL execution right.
        g = request(c, '/api/career-goals/', {'message': '受控取消验证'})
        base = f"/api/career-goals/{g['id']}"; s = request(c, base + '/coding', {'expected_version': 1}); path = base + '/coding/' + s['id']
        sessions.append(s['id']); p = 'ExpiringCache.java'
        code = 'class ExpiringCache { void put(String k,String v,long n,long t){while(true){}} String get(String k,long n){return "MISS";} }'
        s = request(c, path + '/edits', {**binding(s), 'path': p, 'expected_sha256': file_hash(s['workspace']['files'][p]), 'text': code, 'explanation': '合成取消探针'})
        request(c, path + '/public-tests', binding(s)); executor = DockerJava(profile)
        async def running():
            names = (await executor.must(['ps', '-q', '--filter', 'label=interview-agent.executor=ja06'])).decode().split()
            for name in names:
                if b'java -Xms' in (await executor.command(['top', name, '-eo', 'pid,args'])).stdout: return True
            return False
        until = time.monotonic() + 30
        while not asyncio.run(running()):
            if time.monotonic() > until: raise RuntimeError('candidate_Java_not_observed')
            time.sleep(.1)
        s = request(c, path); s = request(c, path + '/cancel', {'expected_version': s['version']})
        assert s['status'] == 'CANCELLED' and s['runs'][-1]['status'] == 'CANCELLED'
        until = time.monotonic() + 12
        while asyncio.run(executor.must(['ps', '-aq', '--filter', 'label=interview-agent.executor=ja06'])).strip():
            if time.monotonic() > until: raise RuntimeError('candidate_cleanup_timeout')
            time.sleep(.2)
        write(out / 'cancel.json', request(c, path)); checks.extend(['cancel:SQL_fence_during_actual_Java', 'cancel:actual_container_cleanup'])
    return {'checks': checks, 'session_ids': sessions}


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--profile', type=Path, default=ROOT / 'configs/ja06-docker-java.json'); args = p.parse_args()
    with socket.socket() as sock: sock.bind(('127.0.0.1', 3100))
    profile = DockerProfile.load(args.profile)
    isolation = asyncio.run(runpy.run_path(str(ROOT / 'scripts/check-executor.py'))['check'](profile))
    owner = DatabaseSettings.model_validate_json((ROOT / '.private/postgres/test-migration.json').read_text(encoding='utf-8'))
    if owner.dbname != 'py_interview_test': raise ValueError('synthetic_demo_requires_test_database')
    asyncio.run(migrate(owner, ROOT / 'backend/migrations'), loop_factory=selector_loop)
    rid = uuid4().hex; out = ROOT / '.private/ja-07-02/http' / rid; out.mkdir(parents=True)
    email = 'ja0702-' + rid + '@example.com'; write(out / 'isolation.json', isolation)
    processes = []; handles = []; error = None
    receipt = {'status': 'failed', 'scope': 'controlled scripted HTTP/API/SQL/Redis/Docker; not live model', 'new_model_http_calls': 0, 'model_key_read': False}
    try:
        for name, command in [('api', [sys.executable, '-m', 'interview_agent.api', '--config', '.private/postgres/test.json', '--coding-executor-profile', str(args.profile)]),
                              ('coding-worker', [sys.executable, '-m', 'interview_agent.coding.worker', '--config', '.private/redis/test-worker.json', '--executor', 'docker', '--profile', str(args.profile), '--provider', 'scripted'])]:
            h = (out / (name + '.log')).open('wb'); handles.append(h)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=h, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0))
        with httpx.Client(trust_env=False, timeout=1) as c:
            until = time.monotonic() + 20
            while True:
                if any(p.poll() is not None for p in processes): raise RuntimeError('owned_service_exited')
                try:
                    if c.get(BASE + '/health').status_code == 200: break
                except httpx.HTTPError: pass
                if time.monotonic() > until: raise RuntimeError('API_startup_timeout')
                time.sleep(.2)
        receipt.update(scenario(email, out, profile)); receipt['status'] = 'passed'
    except Exception as e: error = type(e).__name__ + ': ' + str(e); receipt['error'] = error
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=10)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
        for h in handles: h.close()
        async def cleanup():
            async with await AsyncConnection.connect(owner.conninfo()) as conn:
                return len(await (await conn.execute('DELETE FROM auth.users WHERE email=%s RETURNING id', (email,))).fetchall())
        receipt['cleaned_synthetic_users'] = asyncio.run(cleanup(), loop_factory=selector_loop)
        receipt['owned_processes_stopped'] = all(p.poll() is not None for p in processes)
        receipt['remaining_candidate_containers'] = len(asyncio.run(DockerJava(profile).must(['ps', '-aq', '--filter', 'label=interview-agent.executor=ja06'])).split())
        write(out / 'receipt.json', receipt)
    print(json.dumps({'status': receipt['status'], 'receipt': str(out / 'receipt.json'), 'error': error}, ensure_ascii=False))
    return 0 if receipt['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
