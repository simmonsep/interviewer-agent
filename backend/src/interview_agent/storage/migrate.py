"""Checksummed, transactionally locked Python migrations. No down/reset commands."""

import hashlib
from pathlib import Path

from psycopg import AsyncConnection

from .config import DatabaseSettings


async def migrate(settings: DatabaseSettings, directory: Path) -> list[str]:
    if settings.user != "py_interview_owner":
        raise ValueError("migration_owner_role_required")
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise ValueError("migration_files_missing")
    contents = {p.name: p.read_bytes() for p in files}
    checksums = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    async with await AsyncConnection.connect(settings.conninfo(), autocommit=True) as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7420401)")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS infra")
            await conn.execute("CREATE TABLE IF NOT EXISTS infra.schema_migrations(name text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT clock_timestamp())")
            rows = await (await conn.execute("SELECT name,sha256 FROM infra.schema_migrations ORDER BY name")).fetchall()
            for name, checksum in rows:
                if checksums.get(name) != checksum:
                    raise ValueError("migration_checksum_mismatch")
            applied = {name for name, _ in rows}
            result = []
            for path in files:
                if path.name in applied:
                    continue
                await conn.execute(contents[path.name].decode("utf-8"), prepare=False)
                await conn.execute("INSERT INTO infra.schema_migrations(name,sha256) VALUES(%s,%s)", (path.name, checksums[path.name]))
                result.append(path.name)
            return result
