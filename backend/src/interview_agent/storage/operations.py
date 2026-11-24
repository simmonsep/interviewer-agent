"""SQL is authoritative for commands, retries and fenced commits; Redis is notification only."""

import hashlib
import json
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from interview_agent.contracts.operations import OperationView, QueueMessage, ASSESSMENT_WAIT_CODES
from interview_agent.contracts.resources import UpdateFocus

from .auth import Principal, require_session
from .errors import ApiError
from interview_agent.domain.practice import AssessmentContext, AssessmentOutput, context_hash, gate_candidate
from interview_agent.runtime.types import ScopeError

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
PUBLIC = "id,user_id,resource_id,kind,expected_version,status,result,error_code,created_at,completed_at"


async def append_event(conn, operation_id):
    await conn.execute("INSERT INTO app.business_events(operation_id,user_id,status,error_code) SELECT id,user_id,status,error_code FROM app.operations WHERE id=%s", (operation_id,))


def operation_view(row):
    value = {k: row[k] for k in PUBLIC.split(",")}
    for key in ("id", "user_id", "resource_id"):
        value[key] = str(value[key])
    for key in ("created_at", "completed_at"):
        value[key] = value[key].isoformat() if value[key] is not None else None
    return OperationView.model_validate(value)


@dataclass(frozen=True)
class Claim:
    message: QueueMessage
    user_id: str
    resource_id: str
    attempt: int
    fencing_token: int
    focus: str
    kind: str = "update_target_focus"
    session_id: str | None = None
    session_version: int | None = None


def duration(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 120:
        raise ValueError("invalid_lease_duration")
    return seconds


class OperationRepository:
    def __init__(self, pool):
        self.pool = pool

    async def enqueue(self, principal: Principal, resource_id: str, key: str, value: UpdateFocus):
        digest = hashlib.sha256(json.dumps([resource_id, value.expected_version, value.focus],
                                           ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            target = await (await conn.execute("SELECT version FROM app.training_targets WHERE id=%s AND user_id=%s AND deleted_at IS NULL FOR SHARE",
                                               (resource_id, principal.user_id))).fetchone()
            if target is None:
                raise ApiError("resource_unavailable", 404)
            async def replay():
                row = await (await conn.execute("SELECT * FROM app.operations WHERE user_id=%s AND kind='update_target_focus' AND request_id=%s",
                                                (principal.user_id, key))).fetchone()
                if row is not None and row["request_hash"] != digest:
                    raise ApiError("idempotency_conflict", 409)
                return row
            row = await replay()
            if row is not None:
                return operation_view(row), False
            if target["version"] != value.expected_version:
                raise ApiError("version_conflict", 409)
            row = await (await conn.execute("INSERT INTO app.operations(user_id,resource_id,kind,request_id,request_hash,expected_version,input_focus) VALUES(%s,%s,'update_target_focus',%s,%s,%s,%s) ON CONFLICT(user_id,kind,request_id) DO NOTHING RETURNING *",
                                            (principal.user_id, resource_id, key, digest, value.expected_version, value.focus))).fetchone()
            if row is None:
                return operation_view(await replay()), False
            job = await (await conn.execute("INSERT INTO app.background_jobs(operation_id) VALUES(%s) RETURNING id", (row["id"],))).fetchone()
            await conn.execute("INSERT INTO app.outbox_events(job_id) VALUES(%s)", (job["id"],))
            await append_event(conn, row["id"])
            return operation_view(row), True

    async def get(self, principal: Principal, operation_id: str):
        async with self.pool.connection() as conn:
            await require_session(conn, principal)
            row = await (await conn.execute("SELECT o.* FROM app.operations o JOIN app.training_targets t ON t.id=o.resource_id AND t.user_id=o.user_id WHERE o.id=%s AND o.user_id=%s AND t.deleted_at IS NULL AND (o.session_id IS NULL OR EXISTS(SELECT 1 FROM app.practice_sessions s WHERE s.id=o.session_id AND s.user_id=o.user_id AND s.deleted_at IS NULL))",
                                            (operation_id, principal.user_id))).fetchone()
            if row is None:
                raise ApiError("resource_unavailable", 404)
            return operation_view(row)

    async def cancel(self, principal: Principal, operation_id: str):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            row = await (await conn.execute("SELECT o.* FROM app.operations o JOIN app.training_targets t ON t.id=o.resource_id AND t.user_id=o.user_id WHERE o.id=%s AND o.user_id=%s AND t.deleted_at IS NULL AND (o.session_id IS NULL OR EXISTS(SELECT 1 FROM app.practice_sessions s WHERE s.id=o.session_id AND s.user_id=o.user_id AND s.deleted_at IS NULL)) FOR UPDATE OF o",
                                            (operation_id, principal.user_id))).fetchone()
            if row is None:
                raise ApiError("resource_unavailable", 404)
            if row["status"] not in TERMINAL:
                await conn.execute("UPDATE app.background_jobs SET status='CANCELLED',fencing_token=fencing_token+1,lease_until=NULL,updated_at=clock_timestamp() WHERE operation_id=%s", (operation_id,))
                await self._finish(conn, operation_id, (await (await conn.execute("SELECT id FROM app.background_jobs WHERE operation_id=%s", (operation_id,))).fetchone())["id"], "CANCELLED")
                row = await (await conn.execute("SELECT * FROM app.operations WHERE id=%s", (operation_id,))).fetchone()
            return operation_view(row)

    async def pending_dispatch(self, recovery_seconds=5):
        async with self.pool.connection() as conn:
            return await (await conn.execute("""SELECT e.id AS outbox_id,j.id AS job_id,o.id AS operation_id,o.expected_version AS resource_version
                FROM app.outbox_events e JOIN app.background_jobs j ON j.id=e.job_id JOIN app.operations o ON o.id=j.operation_id
                WHERE o.status IN ('PENDING','RUNNING') AND ((j.status='PENDING' AND j.ready_at<=clock_timestamp()) OR (j.status='RUNNING' AND j.lease_until<=clock_timestamp()))
                AND (e.dispatched_at IS NULL OR e.dispatched_at<=clock_timestamp()-(%s*interval '1 second')) ORDER BY e.created_at,e.id LIMIT 100""", (recovery_seconds,))).fetchall()

    async def mark_dispatched(self, outbox_id):
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE app.outbox_events SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s", (outbox_id,))

    async def _lock(self, conn, message):
        operation = await (await conn.execute("SELECT * FROM app.operations WHERE id=%s FOR UPDATE", (message.operation_id,))).fetchone()
        job = await (await conn.execute("SELECT * FROM app.background_jobs WHERE id=%s AND operation_id=%s FOR UPDATE", (message.job_id, message.operation_id))).fetchone()
        if operation is None or job is None or operation["expected_version"] != message.resource_version:
            return None
        target = await (await conn.execute("SELECT * FROM app.training_targets WHERE id=%s FOR UPDATE", (operation["resource_id"],))).fetchone()
        if operation["session_id"] is not None:
            operation["_session"] = await (await conn.execute("SELECT * FROM app.practice_sessions WHERE id=%s FOR UPDATE", (operation["session_id"],))).fetchone()
        # Time must be read AFTER every potentially blocking row lock, including target.
        times = await (await conn.execute("SELECT lease_until>clock_timestamp() AS active,ready_at<=clock_timestamp() AS ready FROM app.background_jobs WHERE id=%s", (message.job_id,))).fetchone()
        return operation, job, target, times

    @staticmethod
    def _resource_matches(operation, target):
        if not (target is not None and target["user_id"] == operation["user_id"] and target["deleted_at"] is None
                and target["version"] == operation["expected_version"]):
            return False
        if operation["kind"] == "assess_answer":
            session = operation.get("_session")
            return (session is not None and session["user_id"] == operation["user_id"] and session["target_id"] == target["id"]
                    and session["target_version"] == target["version"] and session["deleted_at"] is None
                    and session["version"] == operation["session_version"] and session["status"] == "EVALUATING")
        return True

    @staticmethod
    async def _finish(conn, operation_id, job_id, status, *, code=None, result=None):
        await conn.execute("UPDATE app.operations SET status=%s,error_code=%s,result=%s,completed_at=clock_timestamp() WHERE id=%s",
                           (status, code, Jsonb(result) if result is not None else None, operation_id))
        await conn.execute("UPDATE app.background_jobs SET status=%s,lease_until=NULL,updated_at=clock_timestamp() WHERE id=%s", (status, job_id))
        if status in ("FAILED", "CANCELLED"):
            await conn.execute("UPDATE app.practice_sessions s SET status=%s,version=s.version+1 FROM app.operations o WHERE o.id=%s AND s.id=o.session_id AND s.user_id=o.user_id AND s.version=o.session_version AND s.status='EVALUATING' AND s.deleted_at IS NULL", (status, operation_id))
        await append_event(conn, operation_id)

    async def claim(self, message: QueueMessage, lease_seconds=30):
        duration(lease_seconds)
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._lock(conn, message)
            if locked is None:
                return None
            operation, job, target, times = locked
            if operation["status"] not in ("PENDING", "RUNNING") or job["status"] in TERMINAL:
                return None
            if (job["status"] == "RUNNING" and times["active"]) or not times["ready"]:
                return None
            if not self._resource_matches(operation, target):
                await self._finish(conn, operation["id"], job["id"], "FAILED", code="resource_changed")
                return None
            if job["attempt"] >= 3:
                await self._finish(conn, operation["id"], job["id"], "FAILED", code="attempts_exhausted")
                return None
            row = await (await conn.execute("UPDATE app.background_jobs SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp() WHERE id=%s RETURNING attempt,fencing_token", (lease_seconds, job["id"]))).fetchone()
            await conn.execute("UPDATE app.operations SET status='RUNNING',error_code=NULL WHERE id=%s", (operation["id"],))
            await append_event(conn, operation["id"])
            return Claim(message, str(operation["user_id"]), str(operation["resource_id"]), row["attempt"], row["fencing_token"], operation["input_focus"],
                         operation["kind"], str(operation["session_id"]) if operation["session_id"] else None, operation["session_version"])

    async def _owned(self, conn, claim):
        locked = await self._lock(conn, claim.message)
        if locked is None:
            return None
        operation, job, target, times = locked
        if not (operation["status"] == job["status"] == "RUNNING" and times["active"]
                and job["attempt"] == claim.attempt and job["fencing_token"] == claim.fencing_token
                and str(operation["user_id"]) == claim.user_id and str(operation["resource_id"]) == claim.resource_id
                and operation["input_focus"] == claim.focus and operation["kind"] == claim.kind
                and (str(operation["session_id"]) if operation["session_id"] else None) == claim.session_id
                and operation["session_version"] == claim.session_version):
            return None
        return locked

    async def heartbeat(self, claim: Claim, lease_seconds=30):
        duration(lease_seconds)
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None or not self._resource_matches(locked[0], locked[2]):
                return False
            await conn.execute("UPDATE app.background_jobs SET lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp() WHERE id=%s", (lease_seconds, claim.message.job_id))
            return True

    async def complete(self, claim: Claim):
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None:
                return False
            operation, job, target, _ = locked
            if not self._resource_matches(operation, target):
                await self._finish(conn, operation["id"], job["id"], "FAILED", code="resource_changed")
                return False
            if claim.kind == "assess_answer":
                context = await self._context(conn, claim, locked)
                saved = await self._cached(conn, claim, context)
                if saved is None:
                    raise ScopeError("candidate_cache_missing")
                row = await (await conn.execute("INSERT INTO app.assessment_revisions(user_id,session_id,operation_id,answer_id,candidate,metadata) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id", (claim.user_id, claim.session_id, claim.message.operation_id, context.answer_id, Jsonb(saved.candidate.model_dump(mode="json")), Jsonb(saved.metadata.model_dump(mode="json"))))).fetchone()
                await conn.execute("UPDATE app.practice_sessions SET status='COMPLETED',version=version+1 WHERE id=%s AND user_id=%s AND version=%s AND deleted_at IS NULL", (claim.session_id, claim.user_id, claim.session_version))
                await self._finish(conn, operation["id"], job["id"], "SUCCEEDED", result={"session_id": claim.session_id, "assessment_revision_id": str(row["id"])})
                return True
            row = await (await conn.execute("UPDATE app.training_targets SET focus=%s,version=version+1 WHERE id=%s AND user_id=%s AND version=%s AND deleted_at IS NULL RETURNING id,focus,version,context_mode",
                                            (claim.focus, claim.resource_id, claim.user_id, claim.message.resource_version))).fetchone()
            result = dict(row, id=str(row["id"]))
            await self._finish(conn, operation["id"], job["id"], "SUCCEEDED", result=result)
            return True

    async def fail_attempt(self, claim: Claim):
        if claim.kind == "assess_answer":
            return await self.fail_assessment(claim)
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None:
                return False
            if not self._resource_matches(locked[0], locked[2]):
                await self._finish(conn, claim.message.operation_id, claim.message.job_id, "FAILED", code="resource_changed")
            elif claim.attempt >= 3:
                await self._finish(conn, claim.message.operation_id, claim.message.job_id, "FAILED", code="worker_execution_failed")
            else:
                await conn.execute("UPDATE app.operations SET status='PENDING',error_code='worker_execution_failed' WHERE id=%s", (claim.message.operation_id,))
                await conn.execute("UPDATE app.background_jobs SET status='PENDING',lease_until=NULL,ready_at=clock_timestamp()+interval '1 second',updated_at=clock_timestamp() WHERE id=%s", (claim.message.job_id,))
                await conn.execute("UPDATE app.outbox_events SET dispatched_at=NULL WHERE job_id=%s", (claim.message.job_id,))
                await append_event(conn, claim.message.operation_id)
            return True

    async def can_ack(self, message: QueueMessage):
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT o.status AS operation_status,j.status AS job_status,o.expected_version FROM app.background_jobs j JOIN app.operations o ON o.id=j.operation_id WHERE j.id=%s AND o.id=%s",
                                            (message.job_id, message.operation_id))).fetchone()
            return row is None or row["expected_version"] != message.resource_version or row["operation_status"] in TERMINAL | {"WAITING_CREDENTIALS"} or row["job_status"] in TERMINAL

    async def _context(self, conn, claim, locked):
        operation, _, _, _ = locked
        if claim.kind != "assess_answer" or not self._resource_matches(operation, locked[2]):
            raise ScopeError("resource_changed")
        answer = await (await conn.execute("SELECT id,original_text FROM app.answer_attempts WHERE session_id=%s AND user_id=%s", (claim.session_id, claim.user_id))).fetchone()
        if answer is None:
            raise ScopeError("answer_required")
        return AssessmentContext(user_id=claim.user_id, session_id=claim.session_id, session_version=claim.session_version,
                                 target_id=claim.resource_id, target_version=claim.message.resource_version,
                                 operation_id=claim.message.operation_id, answer_id=str(answer["id"]), answer_text=answer["original_text"],
                                 question=operation["_session"]["question_snapshot"])

    async def assessment_context(self, claim):
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None:
                raise ScopeError("execution_expired")
            return await self._context(conn, claim, locked)

    async def _cached(self, conn, claim, context):
        row = await (await conn.execute("SELECT * FROM app.assessment_candidates WHERE operation_id=%s", (claim.message.operation_id,))).fetchone()
        if row is None:
            return None
        if row["input_hash"] != context_hash(context):
            raise ScopeError("candidate_cache_mismatch")
        return AssessmentOutput(candidate=gate_candidate(context, row["candidate"]), metadata=row["metadata"])

    async def saved_assessment(self, claim):
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None:
                raise ScopeError("execution_expired")
            return await self._cached(conn, claim, await self._context(conn, claim, locked))

    async def save_assessment(self, claim, output):
        output = AssessmentOutput.model_validate(output)
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None or not self._resource_matches(locked[0], locked[2]):
                return False
            context = await self._context(conn, claim, locked)
            candidate = gate_candidate(context, output.candidate)
            await conn.execute("INSERT INTO app.assessment_candidates(operation_id,input_hash,candidate,metadata) VALUES(%s,%s,%s,%s) ON CONFLICT(operation_id) DO NOTHING", (claim.message.operation_id, context_hash(context), Jsonb(candidate.model_dump(mode="json")), Jsonb(output.metadata.model_dump(mode="json"))))
            await self._cached(conn, claim, context)
            return True

    async def fail_assessment(self, claim, *, waiting=False, code="model_not_enabled"):
        if waiting and code not in ASSESSMENT_WAIT_CODES:
            raise ValueError("invalid_assessment_wait_code")
        async with self.pool.connection() as conn, conn.transaction():
            locked = await self._owned(conn, claim)
            if locked is None:
                return False
            if not self._resource_matches(locked[0], locked[2]):
                await self._finish(conn, claim.message.operation_id, claim.message.job_id, "FAILED", code="resource_changed")
            elif waiting:
                await conn.execute("UPDATE app.operations SET status='WAITING_CREDENTIALS',error_code=%s WHERE id=%s", (code, claim.message.operation_id))
                await conn.execute("UPDATE app.background_jobs SET status='PENDING',lease_until=NULL,updated_at=clock_timestamp() WHERE id=%s", (claim.message.job_id,))
                await append_event(conn, claim.message.operation_id)
            else:
                await self._finish(conn, claim.message.operation_id, claim.message.job_id, "FAILED", code="assessment_execution_failed")
            return True

    async def resume(self, principal, operation_id):
        await self.get(principal, operation_id)
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            job = await (await conn.execute("SELECT j.id,o.expected_version FROM app.background_jobs j JOIN app.operations o ON o.id=j.operation_id WHERE o.id=%s AND o.user_id=%s", (operation_id, principal.user_id))).fetchone()
            message = QueueMessage(job_id=str(job["id"]), operation_id=operation_id, resource_version=job["expected_version"])
            locked = await self._lock(conn, message)
            if locked[0]["status"] == "WAITING_CREDENTIALS":
                if not self._resource_matches(locked[0], locked[2]):
                    await self._finish(conn, operation_id, job["id"], "FAILED", code="resource_changed")
                else:
                    await conn.execute("UPDATE app.operations SET status='PENDING',error_code=NULL WHERE id=%s", (operation_id,))
                    await conn.execute("UPDATE app.background_jobs SET ready_at=clock_timestamp() WHERE id=%s", (job["id"],))
                    await conn.execute("UPDATE app.outbox_events SET dispatched_at=NULL WHERE job_id=%s", (job["id"],))
                    await append_event(conn, operation_id)
        return await self.get(principal, operation_id)

    async def events(self, principal, operation_id, after=0):
        await self.get(principal, operation_id)
        async with self.pool.connection() as conn:
            await require_session(conn, principal)
            rows = await (await conn.execute("SELECT e.event_id::text,e.status,e.error_code FROM app.business_events e JOIN app.operations o ON o.id=e.operation_id JOIN app.training_targets t ON t.id=o.resource_id AND t.user_id=o.user_id WHERE e.operation_id=%s AND e.user_id=%s AND e.event_id>%s AND t.deleted_at IS NULL AND (o.session_id IS NULL OR EXISTS(SELECT 1 FROM app.practice_sessions s WHERE s.id=o.session_id AND s.user_id=o.user_id AND s.deleted_at IS NULL)) ORDER BY e.event_id LIMIT 100", (operation_id, principal.user_id, after))).fetchall()
            return {"items": rows, "next_cursor": rows[-1]["event_id"] if rows else str(after)}
