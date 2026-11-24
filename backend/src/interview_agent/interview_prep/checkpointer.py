"""Official LangGraph PostgreSQL checkpointer: explicit init, dedicated schema, restricted role.

Two deliberate deviations from the rest of the project, both documented in the S01 report:

1. The checkpoint tables live in their own schema (`checkpoint`), not `public`, because they are
   the upstream schema (`AsyncPostgresSaver.setup()` owns their shape and their SQL is
   unqualified).
2. The saver connection is the ONE connection that cannot use the project-wide
   `search_path=pg_catalog` hardening, because every upstream query is unqualified. It uses
   `search_path=<schema>,pg_catalog`, so it still cannot resolve `app`/`auth` tables by accident.

Initialization is an explicit operator command and grants the restricted runtime role what it
needs. Running it twice is a no-op.
"""
from __future__ import annotations

import argparse
import re
import sys
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo

SCHEMA = "checkpoint"
DATA_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _check(value, what):
    if not isinstance(value, str) or not ROLE.fullmatch(value):
        raise ValueError(f"invalid_{what}")
    return value


def conninfo(database, schema=SCHEMA):
    """DSN for the checkpointer only: unqualified upstream SQL needs a writable first schema."""
    _check(schema, "schema")
    return make_conninfo(host=database.host, port=database.port, dbname=database.dbname,
                         user=database.user, password=database.password.get_secret_value(),
                         connect_timeout=5,
                         options=f"-c timezone=UTC -c search_path={schema},pg_catalog")


@asynccontextmanager
async def saver(database):
    """One connection per Worker execution; the caller owns and closes it."""
    async with AsyncPostgresSaver.from_conn_string(conninfo(database)) as value:
        yield value


async def initialize(owner, runtime_role):
    """Create/upgrade the official tables in `SCHEMA` and grant the runtime role."""
    role = _check(runtime_role, "runtime_role")
    async with AsyncPostgresSaver.from_conn_string(conninfo(owner)) as value:
        await value.conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION {_check(owner.user, 'owner_role')}")
        await value.setup()
        await value.conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")
        await value.conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(f'{SCHEMA}.{t}' for t in DATA_TABLES)} TO {role}")
        tables = await (await value.conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY 1", (SCHEMA,))).fetchall()
        grants = await (await value.conn.execute(
            "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema=%s AND table_name LIKE 'checkpoint%%' AND grantee=%s ORDER BY 1",
            (SCHEMA, role))).fetchall()
        usage = await (await value.conn.execute(
            "SELECT has_schema_privilege(%s,%s,'USAGE') AS ok", (role, SCHEMA))).fetchone()
    return {"schema": SCHEMA, "tables": [row["tablename"] for row in tables],
            "runtime_role": role, "grants": [row["privilege_type"] for row in grants],
            "schema_usage": bool(usage["ok"])}


def main(argv=None):
    from pathlib import Path
    parser = argparse.ArgumentParser(prog="python -m interview_agent.interview_prep.checkpointer",
                                     description=__doc__)
    parser.add_argument("--owner-config", required=True, type=Path,
                        help="owner (migration) database config; the schema is created as this role")
    parser.add_argument("--runtime-role", required=True,
                        help="restricted role the Worker connects as (e.g. py_interview_app)")
    args = parser.parse_args(argv)
    import asyncio
    import json
    from interview_agent.storage.config import DatabaseSettings
    owner = DatabaseSettings.model_validate_json(Path(args.owner_config).read_text(encoding="utf-8"))
    result = asyncio.run(initialize(owner, args.runtime_role), loop_factory=asyncio.SelectorEventLoop)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
