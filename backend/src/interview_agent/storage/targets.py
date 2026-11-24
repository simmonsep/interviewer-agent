"""Owner-scoped CRUD, per-user idempotency and conditional versions on real SQL."""

import hashlib
import json

from psycopg_pool import AsyncConnectionPool

from interview_agent.contracts.practice import CreateTarget
from interview_agent.contracts.resources import TargetView

from .auth import Principal, require_session
from .errors import ApiError

COLUMNS = "id,user_id,role_package_id,role_package_version,level,focus,context_mode,version,created_at"


def target_view(row) -> TargetView:
    value = {key: row[key] for key in COLUMNS.split(",")}
    value.update(id=str(row["id"]), user_id=str(row["user_id"]), created_at=row["created_at"].isoformat())
    return TargetView.model_validate(value)


class TargetRepository:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def create(self, principal: Principal, request_id: str, value: CreateTarget):
        request_hash = hashlib.sha256(json.dumps([value.role_package_id, value.level, value.focus],
                                               ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await require_session(conn, principal)
                row = await (await conn.execute(
                    f"INSERT INTO app.training_targets(user_id,request_id,request_hash,role_package_id,role_package_version,level,focus) VALUES(%s,%s,%s,%s,'candidate-v1',%s,%s) ON CONFLICT(user_id,request_id) DO NOTHING RETURNING {COLUMNS}",
                    (principal.user_id, request_id, request_hash, value.role_package_id, value.level, value.focus))).fetchone()
                if row is not None:
                    return target_view(row), True
                prior = await (await conn.execute(f"SELECT {COLUMNS},request_hash,deleted_at FROM app.training_targets WHERE user_id=%s AND request_id=%s",
                                                  (principal.user_id, request_id))).fetchone()
                if prior is None or prior["deleted_at"] is not None:
                    raise ApiError("resource_unavailable", 404)
                if prior["request_hash"] != request_hash:
                    raise ApiError("idempotency_conflict", 409)
                return target_view(prior), False

    async def get(self, principal: Principal, resource_id: str):
        async with self.pool.connection() as conn:
            await require_session(conn, principal)
            row = await (await conn.execute(f"SELECT {COLUMNS} FROM app.training_targets WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
                                            (resource_id, principal.user_id))).fetchone()
            if row is None:
                raise ApiError("resource_unavailable", 404)
            return target_view(row)

    async def list(self, principal: Principal):
        async with self.pool.connection() as conn:
            await require_session(conn, principal)
            rows = await (await conn.execute(f"SELECT {COLUMNS} FROM app.training_targets WHERE user_id=%s AND deleted_at IS NULL ORDER BY created_at,id LIMIT 100",
                                             (principal.user_id,))).fetchall()
            return [target_view(row) for row in rows]

    async def change(self, principal: Principal, resource_id: str, expected_version: int, *, focus: str | None = None, delete: bool = False):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await require_session(conn, principal)
                existing = await (await conn.execute("SELECT id FROM app.training_targets WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
                                                     (resource_id, principal.user_id))).fetchone()
                if existing is None:
                    raise ApiError("resource_unavailable", 404)
                assignment = "deleted_at=clock_timestamp()" if delete else "focus=%s"
                params = (resource_id, principal.user_id, expected_version) if delete else (focus, resource_id, principal.user_id, expected_version)
                row = await (await conn.execute(f"UPDATE app.training_targets SET {assignment},version=version+1 WHERE id=%s AND user_id=%s AND version=%s AND deleted_at IS NULL RETURNING {COLUMNS}", params)).fetchone()
                if row is None:
                    raise ApiError("version_conflict", 409)
                return target_view(row)
