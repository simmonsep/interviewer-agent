"""Run the built React page with real FastAPI/SQL and an installed Chromium browser."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from psycopg import AsyncConnection

from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]


async def prepare():
    owner = DatabaseSettings.model_validate_json((ROOT / ".private/postgres/test-migration.json").read_text(encoding="utf-8"))
    assert owner.dbname == "py_interview_test"
    await migrate(owner, ROOT / "backend/migrations")
    return owner


async def cleanup(owner, run_id):
    async with await AsyncConnection.connect(owner.conninfo()) as conn:
        users = [row[0] for row in await (await conn.execute("SELECT id FROM auth.users WHERE email LIKE %s", (f"pyweb-{run_id}-%@example.com",))).fetchall()]
        for user in users:
            for statement in [
                "DELETE FROM app.business_events WHERE user_id=%s",
                "DELETE FROM app.assessment_revisions WHERE user_id=%s",
                "DELETE FROM app.assessment_candidates c USING app.operations o WHERE c.operation_id=o.id AND o.user_id=%s",
                "DELETE FROM app.outbox_events e USING app.background_jobs j,app.operations o WHERE e.job_id=j.id AND j.operation_id=o.id AND o.user_id=%s",
                "DELETE FROM app.background_jobs j USING app.operations o WHERE j.operation_id=o.id AND o.user_id=%s",
                "DELETE FROM app.operations WHERE user_id=%s",
                "DELETE FROM app.answer_attempts WHERE user_id=%s",
                "DELETE FROM app.practice_sessions WHERE user_id=%s",
                "DELETE FROM app.training_targets WHERE user_id=%s",
                "DELETE FROM auth.users WHERE id=%s",
            ]:
                await conn.execute(statement, (user,))
    return len(users)


def wait_ready(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("owned_service_exited; see private logs")
        try:
            if httpx.get(url, timeout=0.5, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("owned_service_start_timeout")


def main():
    run_id = str(uuid4())
    output = ROOT / ".private/py-mig-07/browser" / run_id
    output.mkdir(parents=True)
    for port in (3100, 5174):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))  # Occupied port: fail, never stop an unknown process.
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node_required")
    owner = asyncio.run(prepare(), loop_factory=selector_loop)
    processes, handles = [], []
    code = 1
    try:
        # The same Vite config is used with a test-only middleware serving dist; API proxy is unchanged.
        commands = [
            [sys.executable, "-m", "interview_agent.api", "--config", ".private/postgres/test.json"],
            [node, "apps/web/tests/serve-built.mjs"],
        ]
        for name, command in zip(("api", "web"), commands):
            log = (output / f"{name}.log").open("wb")
            handles.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        wait_ready("http://127.0.0.1:3100/health", processes[0])
        wait_ready("http://127.0.0.1:5174/", processes[1])
        env = {**os.environ, "PY_WEB_RUN_ID": run_id, "PY_WEB_OUTPUT": str(output), "PY_WEB_PYTHON": sys.executable}
        with (output / "browser.log").open("wb") as log:
            result = subprocess.run([node, "apps/web/tests/python-flow.mts"], cwd=ROOT, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=180)
        code = result.returncode
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
        cleaned = asyncio.run(cleanup(owner, run_id), loop_factory=selector_loop)
        (output / "runner.json").write_text(json.dumps({"exit_code": code, "cleaned_synthetic_users": cleaned,
            "owned_processes_stopped": all(p.poll() is not None for p in processes), "model_calls": 0}), encoding="utf-8")
    print(json.dumps({"status": "passed" if code == 0 else "failed", "output": str(output)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
