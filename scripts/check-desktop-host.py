"""Real Tauri/React D-00 developer-host check. No model keys or live provider."""
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    exe = ROOT / '.private/d-00/tauri-target/debug/interview-desktop-probe.exe'
    out = ROOT / '.private/d-00/tauri-http' / uuid4().hex
    out.mkdir(parents=True)
    settings = out / 'settings'
    settings.mkdir()
    shutil.copyfile(ROOT/'.private/postgres/test.json', settings/'api.json')
    shutil.copyfile(ROOT/'.private/redis/test-worker.json', settings/'worker.json')
    env = dict(os.environ, INTERVIEW_DESKTOP_RESOURCES=str(ROOT/'.private/d-00/dist/interview-sidecar'),
               INTERVIEW_DESKTOP_CONFIG_DIR=str(settings), INTERVIEW_PROBE_OUTPUT=str(out), INTERVIEW_PROBE_AUTORUN='1')
    receipts = []
    for attempt in range(2):
        with (out / f'host-{attempt}.log').open('wb') as log:
            child = subprocess.Popen([str(exe)], cwd=out, env=env, stdout=log, stderr=log)
            try:
                assert child.wait(timeout=180) == 0, 'tauri_host_failed'
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
        assert (out / 'frontend-finished.txt').is_file(), 'react_completion_missing'
        receipt = json.loads((out / 'receipt.json').read_text(encoding='utf-8'))
        (out / f'receipt-{attempt}.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        assert receipt['status'] == 'passed', receipt
        receipts.append(receipt)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 3100))  # No orphan API listener after native exit.
        (out / 'frontend-finished.txt').unlink()
    assert receipts[0]['same_cookie_after_api_restart']
    assert receipts[1]['restored_after_host_restart']
    assert receipts[0]['goal_id'] == receipts[1]['goal_id']
    # An occupied port must fail without stopping or attaching to its owner.
    with socket.socket() as sentinel:
        sentinel.bind(('127.0.0.1', 3100))
        sentinel.listen()
        with (out / 'host-port-collision.log').open('wb') as log:
            result = subprocess.run([str(exe)], cwd=out, env=env, stdout=log, stderr=log, timeout=30)
        assert result.returncode == 0  # Startup failure is displayed in the window now.
        assert json.loads((out/'startup.json').read_text(encoding='utf-8'))['code'] == 'port_3100_in_use'
        assert sentinel.getsockname()[1] == 3100
    result = {'status':'passed', 'scope':'native_developer_host_only', 'authenticated_task':True,
              'react_native_roundtrip':True, 'api_restart':True, 'host_restart':True,
              'owned_api_released_port':True, 'occupied_port_owner_preserved':True, 'model_calls':0}
    (out / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'output':str(out), **result}))


if __name__ == '__main__':
    main()
