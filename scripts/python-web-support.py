"""Scoped synthetic browser worker. Default offline test DB; explicit --live uses one approved main-DB authorization."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from interview_agent.api.__main__ import selector_loop
from interview_agent.worker.assessment import scripted_assessment
from interview_agent.worker.config import WorkerSettings
from interview_agent.storage.operations import OperationRepository
from interview_agent.worker.queue import OutboxDispatcher, QueueTransport, TaskWorker
from interview_agent.worker.live import live_assessment
from interview_agent.billing.config import AUTHORIZATION_ID, LivePolicy
from interview_agent.billing.ledger import BillingLedger

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    settings = WorkerSettings.load(ROOT / (".private/redis/worker.json" if args.live else ".private/redis/test-worker.json"))
    assert ((settings.mode, settings.database.dbname, settings.redis.db) ==
            (("development", "py_interview", 0) if args.live else ("test", "py_interview_test", 1)))
    pool = AsyncConnectionPool(settings.database.conninfo(), open=False, kwargs={"row_factory": dict_row})
    redis = settings.redis.connect()
    stream = f"py-interview:{settings.mode}:{uuid4().hex}:jobs:v1"
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            row = await (await conn.execute("""SELECT s.id,o.id AS operation_id,u.email
                FROM app.practice_sessions s JOIN auth.users u ON u.id=s.user_id
                LEFT JOIN app.operations o ON o.session_id=s.id WHERE s.id=%s""", (str(UUID(args.session)),))).fetchone()
        assert row and row["email"].startswith(f"pyweb-{args.run_id}-"), "this_run_synthetic_session_required"
        if args.action == "worker":
            operation_id = str(row["operation_id"])
            class ScopedRepository(OperationRepository):
                async def pending_dispatch(self, recovery_seconds=5):
                    return [r for r in await super().pending_dispatch(recovery_seconds) if str(r["operation_id"]) == operation_id]
            repo, queue = ScopedRepository(pool), QueueTransport(redis, stream)
            async def assess(context, lost):
                await asyncio.sleep(args.delay_ms / 1000)
                return await scripted_assessment(context, lost)
            async def claimed_assess(claim, context, lost):
                return await live_assessment(repo, claim, context, lost,
                    policy=LivePolicy.load(ROOT / "configs/py008-deepseek-flash.json"), authorization_id=AUTHORIZATION_ID,
                    credentials=ROOT / ".private/py-mig-08/credentials.env")
            assert await OutboxDispatcher(repo, queue).dispatch() == 1
            assert await TaskWorker(repo, queue, "py-web-test",
                assess=assess if not args.live and args.assessment == "scripted" else None,
                claimed_assess=claimed_assess if args.live else None).once()
        async with pool.connection() as conn:
            summary = await (await conn.execute("""SELECT s.status,s.version,
                (SELECT count(*) FROM app.answer_attempts a WHERE a.session_id=s.id) AS answers,
                (SELECT count(*) FROM app.assessment_revisions r WHERE r.session_id=s.id) AS revisions,
                (SELECT status FROM app.operations o WHERE o.session_id=s.id) AS operation_status
                FROM app.practice_sessions s WHERE s.id=%s""", (str(row["id"]),))).fetchone()
        audit = ({"billing": await BillingLedger(pool, LivePolicy.load(ROOT / "configs/py008-deepseek-flash.json"), AUTHORIZATION_ID).summary(str(row["operation_id"]))}
                 if args.live else {"model_calls": 0})
        print(json.dumps({**summary, **audit}))
    finally:
        await redis.delete(stream)
        await redis.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["worker", "inspect"])
    parser.add_argument("--session", required=True)
    parser.add_argument("--run-id", required=True, type=lambda value: str(UUID(value)))
    parser.add_argument("--assessment", choices=["disabled", "scripted"], default="scripted")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--live", action="store_true", help="explicit paid execution; fixed PY-MIG-08 main-DB authorization")
    asyncio.run(run(parser.parse_args()), loop_factory=selector_loop)
