"""LangGraph PostgreSQL Store projection of interview evidence (S02).

Business SQL remains the authority. The Store is a *rebuildable* index of finished sessions:

- namespace is `(user_id, goal_id, role, 'interview-observations')`, key is the observation id;
- the value is a small metadata record, never a full resume or answer;
- a retracted or ineligible observation never enters the projection, and every hit must still be
  re-validated against SQL before it can be used (S02b wires that read path).

The Store's own tables live in a dedicated `memory` schema for the same reason the checkpointer
does: upstream SQL is unqualified, so only this connection relaxes `search_path`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.conninfo import make_conninfo

SCHEMA = "memory"
NAMESPACE_SUFFIX = "interview-observations"
ITEM_SCHEMA_VERSION = "interview-memory-v1"
ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# Only observations with a usable reference can become cross-session evidence.
ELIGIBLE_LABELS = ("needs_probe", "supported_in_this_answer")


def _check(value, what):
    if not isinstance(value, str) or not ROLE.fullmatch(value):
        raise ValueError(f"invalid_{what}")
    return value


def conninfo(database, schema=SCHEMA):
    _check(schema, "schema")
    return make_conninfo(host=database.host, port=database.port, dbname=database.dbname,
                         user=database.user, password=database.password.get_secret_value(),
                         connect_timeout=5,
                         options=f"-c timezone=UTC -c search_path={schema},pg_catalog")


@asynccontextmanager
async def store(database):
    async with AsyncPostgresStore.from_conn_string(conninfo(database)) as value:
        yield value


def namespace(user_id, goal_id, role):
    return (str(user_id), str(goal_id), str(role), NAMESPACE_SUFFIX)


def eligible(row):
    """One gate for projection, backfill and the read path."""
    return (row.get("correction") is None and row.get("label") in ELIGIBLE_LABELS
            and bool(row.get("topic_id")) and bool(row.get("answer_id")))


def payload(row, *, session_id, goal_version, role, pack_version, observed_at):
    """Whitelisted metadata for one observation. No answer text, no summary, no resume."""
    return {
        "schema_version": ITEM_SCHEMA_VERSION,
        "session_id": str(session_id),
        "goal_id": str(row["goal_id"]),
        "goal_version": goal_version,
        "role": role,
        "pack_version": pack_version,
        "answer_id": str(row["answer_id"]),
        "topic_id": row["topic_id"],
        "label": row["label"],
        "assisted": bool(row["assisted"]),
        "origin": row["origin"],
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "verification": "training_observation_not_verified_ability",
    }


def plan(session, rows):
    """Pure helper shared by the live projection and the backfill command.

    Returns `(namespace, items, stale_keys)`: retracted/ineligible observations that used to be
    indexed are *removed*, because a leftover Store row must never be usable (S02 §3.4).
    """
    ns = namespace(session["user_id"], session["goal_id"], session["role"])
    items, stale = [], []
    for row in rows:
        if eligible(row):
            items.append((ns, str(row["id"]), payload(
                row, session_id=session["id"], goal_version=session["goal_version"], role=session["role"],
                pack_version=row.get("pack_version"), observed_at=row.get("created_at"))))
        else:
            stale.append((ns, str(row["id"])))
    return ns, items, stale


# Backwards-compatible name for callers that only need the items.
def items_for(session, rows):
    return plan(session, rows)[1]


async def project(value, items, *, stale=()):
    """Write items idempotently (same key overwrites) and drop revoked keys."""
    written = 0
    for ns, key, item in items:
        await value.aput(ns, key, item)
        written += 1
    for ns, key in stale:
        await value.adelete(ns, key)
    return written


async def search(value, ns, *, topics=None, limit=6, offset=0):
    """Topic-filtered search inside one exact namespace. The caller re-validates every hit."""
    hits = await value.asearch(ns, limit=max(limit * 3, limit), offset=offset)
    wanted = set(topics or ())
    selected = []
    for hit in hits:
        item = hit.value or {}
        if wanted and item.get("topic_id") not in wanted:
            continue
        selected.append({"key": hit.key, **item})
        if len(selected) >= limit:
            break
    return selected


async def initialize(owner, runtime_role):
    role = _check(runtime_role, "runtime_role")
    async with AsyncPostgresStore.from_conn_string(conninfo(owner)) as value:
        await value.conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION {_check(owner.user, 'owner_role')}")
        await value.setup()
        await value.conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")
        await value.conn.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {SCHEMA}.store TO {role}")
        tables = await (await value.conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY 1", (SCHEMA,))).fetchall()
        grants = await (await value.conn.execute(
            "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema=%s AND table_name='store' AND grantee=%s ORDER BY 1", (SCHEMA, role))).fetchall()
    return {"schema": SCHEMA, "tables": [row["tablename"] for row in tables],
            "runtime_role": role, "grants": [row["privilege_type"] for row in grants]}


SESSION_SQL = """SELECT s.id,s.goal_id,s.goal_version,g.user_id,s.input_payload#>>'{policy,role}' AS role,
                        s.status FROM app.career_interviews s JOIN app.career_goals g ON g.id=s.goal_id WHERE s.id=%s"""

OBSERVATION_SQL = """SELECT o.id,o.goal_id,o.answer_id,o.created_at,
                            o.payload->>'topic_id' AS topic_id,o.payload->>'label' AS label,
                            COALESCE((o.payload->>'assisted')::boolean,false) AS assisted,
                            o.payload->>'origin' AS origin,o.payload->>'pack_version' AS pack_version,
                            c.reason AS correction
                     FROM app.career_interview_observations o
                     LEFT JOIN app.career_interview_corrections c ON c.observation_id=o.id
                     WHERE o.session_id=%s ORDER BY o.created_at,o.id"""


class MemoryProjector:
    """Best-effort projection of finished sessions; never blocks or fails the business path."""

    def __init__(self, database, tracer=None):
        from interview_agent import observability
        self.database = database
        self.tracer = tracer if tracer is not None else observability.current()

    async def candidates(self, user_id, goal_id, role, *, topics=None, limit=6):
        """Topic-relevant observation ids for one account/goal/role.

        Deliberately raises on a Store problem: the caller records the degradation and falls
        back to the same SQL query, so a broken Store can never silently narrow the candidates.
        """
        async with store(self.database) as value:
            hits = await search(value, namespace(user_id, goal_id, role), topics=topics, limit=limit)
        return [item["key"] for item in hits]

    async def project_session(self, pool, session_id):
        try:
            async with pool.connection() as conn:
                session = await (await conn.execute(SESSION_SQL, (session_id,))).fetchone()
                if not session or session["status"] != "COMPLETED" or not session["role"]:
                    return 0
                rows = await (await conn.execute(OBSERVATION_SQL, (session_id,))).fetchall()
            _, items, stale = plan(session, rows)
            if not items and not stale:
                return 0
            async with store(self.database) as value:
                written = await project(value, items, stale=stale)
            self.tracer.record('memory.projected', stage='memory', session_id=str(session_id),
                               question_count=written)
            return written
        except Exception as error:  # fail-open: SQL rows stay authoritative and the backfill can retry
            self.tracer.record('memory.projection_failed', stage='memory',
                               reason_code=type(error).__name__, session_id=str(session_id))
            return 0


async def _backfill(owner, database, limit):
    """Re-project finished sessions from SQL. Idempotent: repeated runs write the same keys."""
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    async with await AsyncConnection.connect(owner.conninfo(), row_factory=dict_row) as conn:
        sessions = await (await conn.execute(
            "SELECT s.id FROM app.career_interviews s WHERE s.status='COMPLETED' "
            "ORDER BY s.created_at DESC LIMIT %s", (limit,))).fetchall()
    projector = MemoryProjector(database)
    total = 0
    for item in sessions:
        total += await projector.project_session(_PoolShim(owner), item["id"])
    return {"sessions": len(sessions), "items": total}


class _PoolShim:
    """The projector only needs `pool.connection()`; the backfill has a plain connection."""

    def __init__(self, owner):
        self.owner = owner

    def connection(self):
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
        return _ConnectionContext(AsyncConnection.connect(self.owner.conninfo(), row_factory=dict_row))


class _ConnectionContext:
    def __init__(self, awaitable):
        self._awaitable = awaitable

    async def __aenter__(self):
        self._connection = await self._awaitable
        return self._connection

    async def __aexit__(self, *exc):
        await self._connection.close()
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m interview_agent.interviews.memory_store", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create/upgrade the Store tables and grant the runtime role")
    init.add_argument("--owner-config", required=True, type=Path)
    init.add_argument("--runtime-role", required=True)
    fill = sub.add_parser("backfill", help="re-project finished sessions from SQL")
    fill.add_argument("--owner-config", required=True, type=Path)
    fill.add_argument("--runtime-config", required=True, type=Path)
    fill.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    from interview_agent.storage.config import DatabaseSettings, Settings
    owner = DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding="utf-8"))
    if args.command == "init":
        result = asyncio.run(initialize(owner, args.runtime_role), loop_factory=asyncio.SelectorEventLoop)
    else:
        database = Settings.load(args.runtime_config).database
        result = asyncio.run(_backfill(owner, database, args.limit), loop_factory=asyncio.SelectorEventLoop)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
