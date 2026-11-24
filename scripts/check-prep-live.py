"""S03c live harness: ONE planning call, inside an explicit cap, with the ledger summary printed.

This is the only way to spend money in the preparation flow, and it never runs on its own:
- `--cap-cny` is the user's own cap (default 0.2 CNY, hard ceiling 2.0);
- the run authorizes exactly one id, runs the preparation Worker with `--provider live`, and
  prints the plan plus the ledger rows for that authorization;
- it refuses to run when the key is missing, when live is not enabled in model.json, or when the
  authorization already has any call that is not KNOWN (UNKNOWN means "stop and ask a human").

usage:
  python scripts/check-prep-live.py --config .private/postgres/test.json --cap-cny 0.2
"""
import argparse
import asyncio
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from interview_agent.api.__main__ import selector_loop  # noqa: E402
from interview_agent.billing.config import default_policy  # noqa: E402
from interview_agent.billing.ledger import authorize  # noqa: E402
from interview_agent.interviews.billing import InterviewLedger, InterviewPolicy  # noqa: E402
from interview_agent.storage.config import DatabaseSettings, Settings  # noqa: E402


async def _clear_queue(worker_config: Path):
    from interview_agent.worker.config import WorkerSettings
    settings = WorkerSettings.load(worker_config)
    redis = settings.redis.connect()
    try:
        await redis.delete(settings.stream + ':preparations')
    finally:
        await redis.aclose()


def preflight(model_config: Path):
    value = json.loads(model_config.read_text(encoding="utf-8")) if model_config.is_file() else {}
    if not value.get("prep_enabled"):
        raise SystemExit("prep_enabled is not true in model.json: refusing to spend")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="API config the prep Worker should use")
    parser.add_argument("--model-config", type=Path, default=ROOT / "settings/model.json")
    parser.add_argument("--policy", type=Path, default=ROOT / "configs/ja05-deepseek-flash.json")
    parser.add_argument("--cap-cny", type=float, default=0.2)
    parser.add_argument("--authorization", default=None)
    parser.add_argument("--credentials", default="vault", help="'vault' or a key file path")
    args = parser.parse_args()
    if not 0.2 <= args.cap_cny <= 2.0:
        raise SystemExit("cap must be between 0.2 and 2.0 CNY")
    preflight(args.model_config)

    settings = Settings.load(args.config)
    owner = DatabaseSettings.model_validate_json(
        (args.config.parent / "test-migration.json").read_text(encoding="utf-8"))
    policy = InterviewPolicy.load(args.policy)
    identity = args.authorization or f"desktop-prep-{date.today():%Y%m%d}-cny{args.cap_cny:g}"
    asyncio.run(authorize(owner, policy, identity, int(round(args.cap_cny * 1_000_000_000))),
                loop_factory=selector_loop)

    # The preparation itself is created through the real API so ownership/idempotency apply.
    from test_postgres import client_for, signup
    with client_for(settings) as client:
        client.headers["Origin"] = settings.origin
        signup(client)
        goal = client.post("/api/career-goals/",
                           json={"message": "准备Java后端面试，无简历也可以。",
                                 "conditions": {"direction": "Java后端"}},
                           headers={"Idempotency-Key": uuid4().hex}).json()
        prep = client.post(f"/api/career-goals/{goal['id']}/interview-preparations",
                           json={"expected_version": 1, "role": "java-backend", "max_questions": 2},
                           headers={"Idempotency-Key": uuid4().hex}).json()

        worker_config = args.config.parent.parent / "redis" / "test-worker.json"
        # This harness owns the preparation queue for its run: leftover notifications from earlier
        # one-off runs would otherwise be consumed first and the run would look like it did nothing.
        asyncio.run(_clear_queue(worker_config), loop_factory=selector_loop)
        # `--once` consumes one notification; a real deployment loops, so this harness does too.
        # It stops as soon as this preparation leaves PENDING, so a backlog cannot look like a
        # failure, and it never retries a preparation that already failed (that costs money).
        runs = []
        plan = prep
        for _ in range(8):
            if plan["status"] != "PENDING":
                break
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "interview_agent.interview_prep.worker",
                                     "--config", str(worker_config), "--provider", "live",
                                     "--authorization", identity, "--credentials", args.credentials,
                                     "--policy", str(args.policy), "--once"],
                                    cwd=ROOT, capture_output=True, text=True,
                                    env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "backend/src")})
            runs.append({"exit": result.returncode,
                         "stderr": (result.stderr or "").strip().splitlines()[-4:]})
            plan = client.get(f"/api/career-goals/{goal['id']}/interview-preparations/{prep['id']}").json()

    async def ledger():
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(settings.database.conninfo(), open=False, kwargs={"row_factory": dict_row})
        try:
            await pool.open(wait=True)
            return await InterviewLedger(pool, policy, identity).summary()
        finally:
            await pool.close()

    summary = asyncio.run(ledger(), loop_factory=selector_loop)
    print(json.dumps({"authorization": identity, "prep_id": prep["id"], "status": plan["status"],
                      "plan": plan.get("plan"), "error_code": plan.get("error_code"),
                      "worker_runs": runs,
                      "ledger": {"state": summary["authorization_state"], "attempts": summary["http_attempts"],
                                 "usage_status": summary["usage_status"],
                                 "accounted_upper_nano": summary["accounted_upper_nano"]}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
