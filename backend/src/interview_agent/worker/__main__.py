"""Run independently of API: dispatcher, worker, or both for local development."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import RedisError

from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.operations import OperationRepository

from .config import WorkerSettings
from .queue import OutboxDispatcher, QueueTransport, TaskWorker
from .assessment import scripted_assessment
from .live import live_assessment
from interview_agent.billing.config import AUTHORIZATION_ID, LivePolicy
from interview_agent.billing.ledger import BillingLedger


async def run(settings, mode, once, assessment="disabled", *, policy=None, authorization_id=AUTHORIZATION_ID, credentials=None):
    redis = settings.redis.connect()
    pool = AsyncConnectionPool(settings.database.conninfo(), min_size=1, max_size=8, open=False,
                               timeout=5, reconnect_timeout=5, kwargs={"row_factory": dict_row})
    try:
        await pool.open(wait=True, timeout=8)
        queue = QueueTransport(redis, settings.stream)
        repository = OperationRepository(pool)
        dispatcher = OutboxDispatcher(repository, queue)
        async def claimed_assess(claim, context, cancel):
            return await live_assessment(repository, claim, context, cancel, policy=policy,
                                         authorization_id=authorization_id, credentials=credentials)
        worker = TaskWorker(repository, queue, f"python-{uuid4().hex}", lease_seconds=settings.lease_seconds,
                            reclaim_idle_ms=settings.reclaim_idle_ms, assess=scripted_assessment if assessment == "scripted" else None,
                            claimed_assess=claimed_assess if assessment == "live" else None)
        while True:
            try:
                dispatched = await dispatcher.dispatch(settings.recovery_seconds) if mode != "worker" else 0
                received = await worker.once() if mode != "dispatcher" else False
                if once:
                    audit = ({"authorization_http_attempts": (await BillingLedger(pool, policy, authorization_id).summary())["http_attempts"]}
                             if assessment == "live" else {"model_calls": 0})
                    print(json.dumps({"mode": mode, "dispatched": dispatched, "received": received, **audit}))
                    return
            except RedisError:
                if once:
                    raise RuntimeError("redis_unavailable; SQL tasks retained") from None
                print('{"code":"redis_unavailable","tasks_retained_in_sql":true}', flush=True)
            await asyncio.sleep(0.5)
    finally:
        await redis.aclose()
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Python Streams/SQL worker; live requires explicit policy, credentials and SQL authorization")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=["dispatcher", "worker", "both"], default="both")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--assessment", choices=["disabled", "scripted", "live"], default="disabled")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--policy", type=Path, default=Path("configs/py008-deepseek-flash.json"))
    parser.add_argument("--authorization", default=AUTHORIZATION_ID)
    args = parser.parse_args()
    if args.assessment == "live" and args.credentials is None:
        parser.error("--assessment live requires explicit --credentials")
    settings = WorkerSettings.load(args.config)
    if args.assessment == "live" and settings.mode != "development":
        parser.error("live CLI uses the development database authorization; test database is offline-only")
    try:
        asyncio.run(run(settings, args.mode, args.once, args.assessment,
                        policy=LivePolicy.load(args.policy) if args.assessment == "live" else None,
                        authorization_id=args.authorization, credentials=args.credentials), loop_factory=selector_loop)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
