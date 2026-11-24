"""One explicitly authorized real-model browser flow. All attempts share the persistent CNY 2 authorization."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from uuid import uuid4

from interview_agent.api.__main__ import selector_loop
from interview_agent.billing.config import AUTHORIZATION_ID, LivePolicy, load_key
from interview_agent.billing.ledger import BillingLedger
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("python_web_runner", ROOT / "scripts/check-python-web.py")
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


async def prepare():
    owner = DatabaseSettings.model_validate_json((ROOT / ".private/postgres/migration.json").read_text(encoding="utf-8"))
    assert owner.dbname == "py_interview"
    await migrate(owner, ROOT / "backend/migrations")
    app = DatabaseSettings.model_validate_json(json.dumps(json.loads((ROOT / ".private/postgres/app.json").read_text(encoding="utf-8"))["database"]))
    pool = AsyncConnectionPool(app.conninfo(), open=False, kwargs={"row_factory": dict_row})
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT policy_hash,max_nano,state FROM app.model_authorizations WHERE id=%s", (AUTHORIZATION_ID,))).fetchone()
        policy = LivePolicy.load(ROOT / "configs/py008-deepseek-flash.json")
        assert row and row["policy_hash"] == policy.digest and row["max_nano"] == 2_000_000_000 and row["state"] == "ACTIVE", "explicit_SQL_authorization_required"
        return owner, await BillingLedger(pool, policy, AUTHORIZATION_ID).summary()
    finally:
        await pool.close()


def main():
    # File is only inspected for availability. Its value never enters argv/environment/logs.
    load_key(ROOT / ".private/py-mig-08/credentials.env")
    for port in (3100, 5174):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node_required")
    owner, before = asyncio.run(prepare(), loop_factory=selector_loop)
    run_id = str(uuid4())
    output = ROOT / ".private/py-mig-08/live" / run_id
    output.mkdir(parents=True)
    (output / "billing-before.json").write_text(json.dumps(before), encoding="utf-8")
    processes, handles = [], []
    code = 1
    try:
        for name, command in [
            ("api", [sys.executable, "-m", "interview_agent.api", "--config", ".private/postgres/app.json", "--assessment-mode", "live"]),
            ("web", [node, "apps/web/tests/serve-built.mjs"]),
        ]:
            log = (output / f"{name}.log").open("wb");handles.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        web.wait_ready("http://127.0.0.1:3100/health", processes[0])
        web.wait_ready("http://127.0.0.1:5174/", processes[1])
        env = {**os.environ, "PY_WEB_RUN_ID": run_id, "PY_WEB_OUTPUT": str(output), "PY_WEB_PYTHON": sys.executable}
        with (output / "browser.log").open("wb") as log:
            code = subprocess.run([node, "apps/web/tests/python-live-flow.mts"], cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT, timeout=240).returncode
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
        for log in handles:
            log.close()
        # Accounting has no cascading FK: deleting synthetic practice data never refunds spend.
        cleaned = asyncio.run(web.cleanup(owner, run_id), loop_factory=selector_loop)
        (output / "runner.json").write_text(json.dumps({"exit_code": code, "cleaned_synthetic_users": cleaned,
            "owned_processes_stopped": all(p.poll() is not None for p in processes),
            "authorization_id": AUTHORIZATION_ID, "accounting_retained": True}), encoding="utf-8")
    print(json.dumps({"status": "passed" if code == 0 else "failed", "output": str(output)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
