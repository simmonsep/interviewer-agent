"""D-00 Python packaging seam: frozen API/Worker, actual HTTP/SQL/Redis, free only."""
import json
from pathlib import Path
import secrets
import socket
import subprocess
import time
from uuid import uuid4
import httpx
from interview_agent.evaluation.adapters import arun, cleanup
from interview_agent.storage.config import DatabaseSettings
from interview_agent.evaluation.__main__ import ledger

ROOT = Path(__file__).resolve().parents[1]


def main():
    exe = ROOT / '.private/d-00/dist/interview-sidecar/interview-sidecar.exe'
    config = ROOT / '.private/postgres/test.json'
    worker = ROOT / '.private/redis/test-worker.json'
    owner = DatabaseSettings.model_validate_json((ROOT / '.private/postgres/test-migration.json').read_text(encoding='utf-8'))
    assert owner.dbname == 'py_interview_test'
    with socket.socket() as port:
        port.bind(('127.0.0.1', 3100))
    out = ROOT / '.private/d-00/http' / uuid4().hex
    out.mkdir(parents=True)
    before = arun(ledger(owner))
    process = None
    logs = []
    uid = None
    receipt = {'status': 'failed', 'scope': 'frozen_python_only_not_tauri', 'model_calls': 0}
    try:
        def start():
            log = (out / ('api-' + str(len(logs)) + '.log')).open('wb')
            logs.append(log)
            child = subprocess.Popen([str(exe), 'serve', '--config', str(config)], cwd=out, stdout=log, stderr=log)
            for _ in range(180):
                if child.poll() is not None:
                    raise RuntimeError('frozen_api_exited_early')
                try:
                    if httpx.get('http://127.0.0.1:3100/health', trust_env=False, timeout=1).status_code == 200:
                        return child
                except httpx.TransportError:
                    pass
                time.sleep(.25)
            child.terminate()
            child.wait(timeout=10)
            raise RuntimeError('frozen_api_timeout')

        process = start()
        with httpx.Client(base_url='http://127.0.0.1:3100', headers={'Origin': 'http://127.0.0.1:3100'}, trust_env=False, timeout=15) as c:
            def get(path):
                r = c.get(path)
                r.raise_for_status()
                return r.json()
            def post(path, body):
                r = c.post(path, json=body, headers={'Idempotency-Key': uuid4().hex})
                r.raise_for_status()
                return r.json()
            user = post('/api/auth/sign-up/email', {'email': 'd00-' + uuid4().hex + '@example.com', 'name': 'Synthetic desktop probe', 'password': secrets.token_urlsafe(24)})
            uid = user['user']['id']
            goal = post('/api/career-goals/', {'message': '我想找实习，方向还不明确。'})
            path = '/api/career-goals/' + goal['id']
            subprocess.run([str(exe), 'probe-worker', '--config', str(worker), '--goal-id', goal['id']], cwd=out,
                           stdout=logs[0], stderr=logs[0], check=True, timeout=60)
            result = get(path)
            assert result['action']['type'] == 'ask_user'
            process.terminate()
            process.wait(timeout=10)
            process = start()
            restored = get(path)
            assert restored == result
            receipt.update(status='passed', action=result['action']['type'], sql_restored_after_process_restart=True,
                           cwd_outside_source_tree=True, authenticated_http=True)
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        for log in logs:
            log.close()
        if uid:
            arun(cleanup(owner, uid))
        receipt['model_ledger_unchanged'] = arun(ledger(owner)) == before
        (out / 'receipt.json').write_bytes((json.dumps(receipt, indent=2) + '\n').encode())
        print(json.dumps({'output': str(out), **receipt}))


if __name__ == '__main__':
    main()
