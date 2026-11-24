"""S01 built-page acceptance: plan -> human confirmation -> one session, synthetic account only."""
import argparse
import asyncio
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import socket
import subprocess
import sys
from uuid import uuid4

from interview_agent.api.__main__ import selector_loop
from interview_agent.interview_prep import checkpointer
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('web_runner', ROOT / 'scripts/check-python-web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


async def drop_local_accounts(owner, started):
    """The desktop shell creates `local-<uuid>@desktop.example.com`; drop only this run's."""
    from psycopg import AsyncConnection
    async with await AsyncConnection.connect(owner.conninfo()) as conn:
        cursor = await conn.execute(
            "DELETE FROM auth.users WHERE email LIKE 'local-%%@desktop.example.com' AND created_at >= %s RETURNING id",
            (started,))
        return len(await cursor.fetchall())


async def prepare():
    owner = DatabaseSettings.model_validate_json(
        (ROOT / '.private/postgres/test-migration.json').read_text(encoding='utf-8'))
    assert owner.dbname == 'py_interview_test'
    await migrate(owner, ROOT / 'backend/migrations')
    await checkpointer.initialize(owner, 'py_interview_app')
    return owner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node', help='node executable (this shell may not have it on PATH)')
    parser.add_argument('--script', default='apps/web/tests/prep-flow.mts',
                        help='acceptance script to run (default: the prep pipeline)')
    parser.add_argument('--model-config', default=None,
                        help='start the API with this model.json so the settings pane exists; '
                             'a fake key is seeded when the file is absent')
    args = parser.parse_args()
    for port in (3100, 5174):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    owner = asyncio.run(prepare(), loop_factory=selector_loop)
    run_id = str(uuid4())
    started = datetime.now(timezone.utc)
    output = ROOT / '.private' / 's01' / 'web' / run_id
    output.mkdir(parents=True)
    assert (ROOT / 'apps/web/dist/index.html').is_file(), 'run the web build first'
    api = [sys.executable, '-m', 'interview_agent.api', '--config', '.private/postgres/test.json']
    if args.model_config:
        # The settings pane is only mounted when the API has an external model config. The key is
        # fake on purpose: this run only saves settings, it never calls a provider.
        model_config = ROOT / args.model_config
        if not model_config.is_file():
            model_config.parent.mkdir(parents=True, exist_ok=True)
            model_config.write_text(json.dumps({'provider': 'deepseek',
                                                'base_url': 'https://api.deepseek.com',
                                                'model': 'deepseek-chat',
                                                'api_key': 'offline-not-real'}, indent=1),
                                    encoding='utf-8')
        api += ['--model-config', str(args.model_config)]
    processes, handles, code = [], [], 1
    try:
        for name, command in [('api', api),
                              ('web', [args.node, 'apps/web/tests/serve-built.mjs'])]:
            handle = (output / f'{name}.log').open('wb')
            handles.append(handle)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                              creationflags=subprocess.CREATE_NO_WINDOW))
        web.wait_ready('http://127.0.0.1:3100/health', processes[0])
        web.wait_ready('http://127.0.0.1:5174/', processes[1])
        env = {**os.environ, 'S01_RUN_ID': run_id, 'S01_OUTPUT': str(output), 'S01_PYTHON': sys.executable,
               'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}
        with (output / 'browser.log').open('wb') as log:
            code = subprocess.run([args.node, args.script], cwd=ROOT, env=env,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=900).returncode
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for handle in handles:
            handle.close()
        cleaned = asyncio.run(web.cleanup(owner, run_id), loop_factory=selector_loop)
        cleaned += asyncio.run(drop_local_accounts(owner, started), loop_factory=selector_loop)
        (output / 'runner.json').write_text(json.dumps({
            'exit_code': code, 'cleaned_synthetic_users': cleaned,
            'owned_processes_stopped': all(p.poll() is not None for p in processes)}), encoding='utf-8')
    print(json.dumps({'status': 'passed' if code == 0 else 'failed', 'output': str(output)}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
