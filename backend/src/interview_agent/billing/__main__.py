"""Explicit owner authorization setup or read-only budget summary; never sends model HTTP."""

import argparse
import asyncio
import json
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import DatabaseSettings
from .config import AUTHORIZATION_ID, LivePolicy
from .ledger import BillingLedger, authorize, resume_held


async def run(args):
    policy = LivePolicy.load(args.policy)
    raw = json.loads(args.database.read_text(encoding="utf-8"))
    database = DatabaseSettings.model_validate(raw.get("database", raw))
    if args.authorization == AUTHORIZATION_ID and database.dbname != "py_interview":
        raise ValueError("live_authorization_requires_development_database")
    if args.action == "authorize":
        await authorize(database, policy, args.authorization)
        print(json.dumps({"authorization_id": args.authorization, "max_cny": "2.00", "requests_sent_by_command": 0}))
        return
    if args.action == "resume-held":
        if not args.keep_unknown_reservations:
            raise ValueError("explicit_keep_unknown_reservations_required")
        await resume_held(database, policy, args.authorization)
        print(json.dumps({"authorization_id": args.authorization, "unknown_reservations_retained": True,
                          "old_operations_not_replayed": True, "requests_sent_by_command": 0}))
        return
    pool = AsyncConnectionPool(database.conninfo(), open=False, kwargs={"row_factory": dict_row})
    try:
        await pool.open(wait=True)
        print(json.dumps(await BillingLedger(pool, policy, args.authorization).summary(), ensure_ascii=False))
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["authorize", "summary", "resume-held"])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("configs/py008-deepseek-flash.json"))
    parser.add_argument("--authorization", default=AUTHORIZATION_ID)
    parser.add_argument("--keep-unknown-reservations", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args), loop_factory=selector_loop)


if __name__ == "__main__":
    main()
