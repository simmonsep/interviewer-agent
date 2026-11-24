"""Global authorization lock precedes execution locks. No transaction spans HTTP."""

from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from interview_agent.domain.practice import context_hash
from interview_agent.storage.operations import OperationRepository

from .config import MAX_AUTHORIZED_NANO


class BillingUnavailable(Exception):
    """Admission refusal. `code` is the stable contract; `limit` names which fuse blew.

    F07Z-104 (arbitration Q3): five different guards all answer `model_budget_exhausted`,
    which told the user nothing about what to change. The code stays exactly as it was so
    the existing assertions and API contract keep working; `limit` is the optional detail
    (e.g. `authorization_http_limit`) that the worker records in `run.result`.
    """

    def __init__(self, code, limit=None):
        self.code = code
        self.limit = limit
        super().__init__(code)


async def authorize(owner, policy, authorization_id, max_nano=MAX_AUTHORIZED_NANO):
    if owner.user != "py_interview_owner" or type(max_nano) is not int or not 0 < max_nano <= MAX_AUTHORIZED_NANO:
        raise ValueError("invalid_authorization")
    async with await AsyncConnection.connect(owner.conninfo()) as conn:
        await conn.execute("INSERT INTO app.model_authorizations(id,policy_hash,max_nano) VALUES(%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                           (authorization_id, policy.digest, max_nano))
        row = await (await conn.execute("SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s", (authorization_id,))).fetchone()
        if row != (policy.digest, max_nano):
            raise ValueError("authorization_conflict")


async def resume_held(owner, policy, authorization_id):
    """Explicit operator action: keep every uncertain reservation and request count.

    It does not reconcile/refund an old call or permit replay of its operation.
    """
    if owner.user != "py_interview_owner":
        raise ValueError("authorization_owner_required")
    async with await AsyncConnection.connect(owner.conninfo()) as conn:
        row = await (await conn.execute("SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s FOR UPDATE", (authorization_id,))).fetchone()
        if row is None or row[0] != policy.digest or row[1] > MAX_AUTHORIZED_NANO:
            raise ValueError("authorization_conflict")
        await conn.execute("UPDATE app.model_authorizations SET state='ACTIVE' WHERE id=%s", (authorization_id,))


def usage_upper(policy, usage):
    if not isinstance(usage, dict):
        raise ValueError("usage_unavailable")
    counts = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("usage_invalid")
        counts[key] = value
    prompt, output = counts["prompt_tokens"], counts["completion_tokens"]
    if counts["total_tokens"] != prompt + output or prompt > policy.input_reservation_tokens or output > policy.max_output_tokens:
        raise ValueError("usage_outside_reservation")
    if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(key)
            if type(value) is not int or value < 0:
                raise ValueError("usage_invalid")
            counts[key] = value
        if counts["prompt_cache_hit_tokens"] + counts["prompt_cache_miss_tokens"] != prompt:
            raise ValueError("usage_invalid")
        hit, miss = counts["prompt_cache_hit_tokens"], counts["prompt_cache_miss_tokens"]
    else:
        hit, miss = 0, prompt  # Missing cache detail uses the more expensive rate, not zero.
    upper = hit * policy.input_hit_nano_cny_per_token + miss * policy.input_miss_nano_cny_per_token + output * policy.output_nano_cny_per_token
    return counts, upper


class BillingLedger:
    def __init__(self, pool, policy, authorization_id):
        self.pool, self.policy, self.authorization_id = pool, policy, authorization_id

    async def reserve(self, claim, context, payload_hash, payload_bytes):
        async with self.pool.connection() as conn, conn.transaction():
            authorization = await (await conn.execute("SELECT * FROM app.model_authorizations WHERE id=%s FOR UPDATE", (self.authorization_id,))).fetchone()
            if authorization is None or authorization["policy_hash"] != self.policy.digest:
                raise BillingUnavailable("model_authorization_unavailable")
            if authorization["state"] != "ACTIVE":
                raise BillingUnavailable("model_usage_unknown")
            repo = OperationRepository(self.pool)
            locked = await repo._owned(conn, claim)
            if locked is None or not repo._resource_matches(locked[0], locked[2]):
                raise BillingUnavailable("model_execution_expired")
            current = await repo._context(conn, claim, locked)
            if context_hash(current) != context_hash(context):
                raise BillingUnavailable("model_context_changed")
            previous = await (await conn.execute("SELECT authorization_id,state FROM app.model_http_calls WHERE operation_id=%s", (context.operation_id,))).fetchall()
            if any(row["authorization_id"] != self.authorization_id for row in previous):
                raise BillingUnavailable("model_authorization_mismatch")
            if any(row["state"] != "KNOWN" for row in previous):
                raise BillingUnavailable("model_usage_unknown")
            totals = await (await conn.execute("SELECT count(*) AS n,COALESCE(sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END),0) AS used FROM app.model_http_calls WHERE authorization_id=%s", (self.authorization_id,))).fetchone()
            if (totals["n"] >= self.policy.max_http_attempts or len(previous) >= self.policy.max_operation_attempts
                    or totals["used"] + self.policy.reservation_nano > authorization["max_nano"]):
                raise BillingUnavailable("model_budget_exhausted")
            call_id = str(uuid4())
            await conn.execute("""INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,fencing_token,
                input_hash,payload_hash,payload_bytes,reserved_nano) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (call_id, self.authorization_id, context.operation_id, claim.message.job_id, claim.attempt, claim.fencing_token,
                 context_hash(context), payload_hash, payload_bytes, self.policy.reservation_nano))
            return call_id

    async def settle(self, call_id, *, http_status=None, usage=None, response_model=None, error_code=None, response_meta=None):
        counts, upper = None, None
        if http_status == 200 and error_code is None:
            try:
                counts, upper = usage_upper(self.policy, usage)
            except ValueError:
                error_code = "usage_unavailable"
        known = counts is not None
        # Retain observed numeric fields even when incomplete; unknown is never zero.
        observed = counts if known else ({key: value for key, value in usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
            and type(value) is int and 0 <= value <= 1000000000} if isinstance(usage, dict) else None)
        safe_meta = None
        if isinstance(response_meta, dict):
            safe_meta = {key: value[:160] for key, value in response_meta.items()
                         if key in {"content_type", "content_encoding", "request_id"} and isinstance(value, str)}
            if type(response_meta.get("decoded_bytes")) is int:
                safe_meta["decoded_bytes"] = response_meta["decoded_bytes"]
            if type(response_meta.get("json_object")) is bool:
                safe_meta["json_object"] = response_meta["json_object"]
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT id FROM app.model_authorizations WHERE id=%s FOR UPDATE", (self.authorization_id,))
            row = await (await conn.execute("SELECT * FROM app.model_http_calls WHERE id=%s AND authorization_id=%s FOR UPDATE", (call_id, self.authorization_id))).fetchone()
            if row is None:
                raise ValueError("model_call_unavailable")
            if row["state"] != "RESERVED":
                return row["state"] == "KNOWN"  # Never release an uncertain call by replaying settlement.
            await conn.execute("""UPDATE app.model_http_calls SET state=%s,accounted_nano=%s,raw_usage=%s,http_status=%s,
                response_model=%s,error_code=%s,response_meta=%s,settled_at=clock_timestamp() WHERE id=%s""",
                ("KNOWN" if known else "UNKNOWN", upper, Jsonb(observed) if observed is not None else None, http_status,
                 response_model[:120] if isinstance(response_model, str) else None, None if known else error_code or "request_outcome_unknown",
                 Jsonb(safe_meta) if safe_meta is not None else None, call_id))
            if not known:
                await conn.execute("UPDATE app.model_authorizations SET state='PAUSED' WHERE id=%s", (self.authorization_id,))
        return known

    async def summary(self, operation_id=None):
        async with self.pool.connection() as conn:
            authorization = await (await conn.execute("SELECT max_nano,state FROM app.model_authorizations WHERE id=%s", (self.authorization_id,))).fetchone()
            rows = await (await conn.execute("""SELECT id::text,operation_id::text,state,reserved_nano,accounted_nano,
                raw_usage,http_status,response_model,error_code,response_meta FROM app.model_http_calls WHERE authorization_id=%s
                AND (%s::uuid IS NULL OR operation_id=%s::uuid) ORDER BY created_at,id""",
                (self.authorization_id, operation_id, operation_id))).fetchall()
        accounted = sum(r["accounted_nano"] if r["state"] == "KNOWN" else r["reserved_nano"] for r in rows)
        return {"authorization_id": self.authorization_id,
                "authorization_state": authorization["state"] if authorization else None,
                "max_nano": authorization["max_nano"] if authorization else None,
                "scope": "operation" if operation_id is not None else "authorization",
                "http_attempts": len(rows),
                "usage_status": "unknown" if any(r["state"] != "KNOWN" for r in rows) else "known" if rows else "not_called",
                "accounted_upper_nano": accounted, "calls": rows}
