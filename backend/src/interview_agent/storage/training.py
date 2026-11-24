"""Original answers and question snapshots live in SQL; public views omit private rubrics."""

import hashlib

from psycopg.types.json import Jsonb

from interview_agent.contracts.practice import PracticeSessionView, QuestionSnapshot
from interview_agent.data.catalog import public_question
from interview_agent.domain.practice import feedback_view
from interview_agent.runtime.store import canonical

from .auth import require_session
from .errors import ApiError
from .operations import append_event, operation_view

VIEW_QUERY = """SELECT s.*,a.id AS answer_id,a.original_text,o.id AS operation_id,o.status AS operation_status,o.error_code,
 r.candidate,r.metadata FROM app.practice_sessions s JOIN app.training_targets t ON t.id=s.target_id AND t.user_id=s.user_id
 LEFT JOIN app.answer_attempts a ON a.session_id=s.id AND a.user_id=s.user_id
 LEFT JOIN app.operations o ON o.session_id=s.id AND o.user_id=s.user_id
 LEFT JOIN app.assessment_revisions r ON r.session_id=s.id AND r.user_id=s.user_id
 WHERE s.user_id=%s AND s.deleted_at IS NULL AND t.deleted_at IS NULL"""


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def session_view(row):
    question = QuestionSnapshot.model_validate(row["question_snapshot"])
    return PracticeSessionView.model_validate({
        "id": str(row["id"]), "version": row["version"], "status": row["status"], "mode": "practice", "context_mode": "role_only",
        "target_id": str(row["target_id"]), "question": public_question(question).model_dump(mode="json"), "created_at": row["created_at"].isoformat(),
        "answer": {"id": str(row["answer_id"]), "text": row["original_text"], "origin": "user_submitted"} if row["answer_id"] else None,
        "operation": {"id": str(row["operation_id"]), "status": row["operation_status"], "error_code": row["error_code"]} if row["operation_id"] else None,
        "feedback": feedback_view(question, row["candidate"], row["metadata"]).model_dump(mode="json") if row["status"] == "COMPLETED" and row["candidate"] else None,
    })


class TrainingRepository:
    def __init__(self, pool):
        self.pool = pool

    async def create(self, principal, target_id, key, question: QuestionSnapshot):
        request_hash = digest([target_id, question.id])
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            target = await (await conn.execute("SELECT version FROM app.training_targets WHERE id=%s AND user_id=%s AND deleted_at IS NULL FOR SHARE", (target_id, principal.user_id))).fetchone()
            if target is None:
                raise ApiError("resource_unavailable", 404)
            row = await (await conn.execute("INSERT INTO app.practice_sessions(user_id,target_id,target_version,request_id,request_hash,question_snapshot) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,request_id) DO NOTHING RETURNING id", (principal.user_id, target_id, target["version"], key, request_hash, Jsonb(question.model_dump(mode="json"))))).fetchone()
            if row:
                return str(row["id"]), True
            prior = await (await conn.execute("SELECT id,request_hash,deleted_at FROM app.practice_sessions WHERE user_id=%s AND request_id=%s", (principal.user_id, key))).fetchone()
            if prior["deleted_at"] is not None:
                raise ApiError("resource_unavailable", 404)
            if prior["request_hash"] != request_hash:
                raise ApiError("idempotency_conflict", 409)
            return str(prior["id"]), False

    async def get(self, principal, session_id):
        async with self.pool.connection() as conn:
            await require_session(conn, principal)
            row = await (await conn.execute(VIEW_QUERY + " AND s.id=%s", (principal.user_id, session_id))).fetchone()
            if row is None:
                raise ApiError("resource_unavailable", 404)
            return session_view(row)

    async def list(self, principal):
        async with self.pool.connection() as conn:
            await require_session(conn, principal)
            rows = await (await conn.execute(VIEW_QUERY + " ORDER BY s.created_at DESC,s.id LIMIT 50", (principal.user_id,))).fetchall()
            return [session_view(row) for row in rows]

    async def _session_lock(self, conn, principal, session_id):
        ref = await (await conn.execute("SELECT target_id FROM app.practice_sessions WHERE id=%s AND user_id=%s", (session_id, principal.user_id))).fetchone()
        if ref is None:
            raise ApiError("resource_unavailable", 404)
        target = await (await conn.execute("SELECT * FROM app.training_targets WHERE id=%s AND user_id=%s FOR SHARE", (ref["target_id"], principal.user_id))).fetchone()
        session = await (await conn.execute("SELECT * FROM app.practice_sessions WHERE id=%s AND user_id=%s FOR UPDATE", (session_id, principal.user_id))).fetchone()
        if target is None or target["deleted_at"] is not None or session is None or session["deleted_at"] is not None:
            raise ApiError("resource_unavailable", 404)
        return session, target

    async def submit(self, principal, session_id, key, value):
        request_hash = digest([session_id, value.expected_version, value.text])
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            session, target = await self._session_lock(conn, principal, session_id)
            prior = await (await conn.execute("SELECT request_id,request_hash FROM app.answer_attempts WHERE session_id=%s AND user_id=%s", (session_id, principal.user_id))).fetchone()
            if prior:
                if prior["request_id"] != key or prior["request_hash"] != request_hash:
                    raise ApiError("answer_already_submitted", 409)
                row = await (await conn.execute("SELECT * FROM app.operations WHERE session_id=%s AND user_id=%s", (session_id, principal.user_id))).fetchone()
                return operation_view(row)
            if session["status"] != "WAITING_FOR_ANSWER" or session["version"] != value.expected_version or session["target_version"] != target["version"]:
                raise ApiError("version_conflict", 409)
            await conn.execute("INSERT INTO app.answer_attempts(user_id,session_id,request_id,request_hash,original_text) VALUES(%s,%s,%s,%s,%s)", (principal.user_id, session_id, key, request_hash, value.text))
            await conn.execute("UPDATE app.practice_sessions SET status='EVALUATING',version=version+1 WHERE id=%s", (session_id,))
            row = await (await conn.execute("INSERT INTO app.operations(user_id,resource_id,kind,request_id,request_hash,expected_version,session_id,session_version) VALUES(%s,%s,'assess_answer',%s,%s,%s,%s,%s) ON CONFLICT(user_id,kind,request_id) DO NOTHING RETURNING *", (principal.user_id, session["target_id"], key, request_hash, session["target_version"], session_id, session["version"]+1))).fetchone()
            if row is None:
                raise ApiError("idempotency_conflict", 409)
            job = await (await conn.execute("INSERT INTO app.background_jobs(operation_id) VALUES(%s) RETURNING id", (row["id"],))).fetchone()
            await conn.execute("INSERT INTO app.outbox_events(job_id) VALUES(%s)", (job["id"],))
            await append_event(conn, row["id"])
            return operation_view(row)

    async def delete(self, principal, session_id, expected_version):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            session, _ = await self._session_lock(conn, principal, session_id)
            if session["version"] != expected_version:
                raise ApiError("version_conflict", 409)
            # No op/job lock here: deletion invalidates the session; worker rechecks it.
            await conn.execute("UPDATE app.practice_sessions SET deleted_at=clock_timestamp(),version=version+1 WHERE id=%s", (session_id,))
            return {"id": session_id, "deleted": True, "version": expected_version+1}
