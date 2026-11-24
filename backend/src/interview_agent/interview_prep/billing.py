"""Billing for the preparation planner (S03b).

Same authorization row, same caps and the same ledger semantics as the interview role; the only
difference is what counts as "still mine": a preparation row instead of an interview session.
A planning call is admitted only when this exact attempt/fencing token still owns the row, the
goal is inside its training budget, and the authorization has room for one more reservation.
"""
from uuid import uuid4

from interview_agent.billing.ledger import BillingLedger, BillingUnavailable
from interview_agent.career.repository import digest
from interview_agent.career.budget import legacy_budget_exhausted

PURPOSE = 'career_interview_prep'
MAX_PREPARATION_REQUESTS = 3
# The interview role's `policy.max_http_attempts` (12) describes ONE session's ceiling. Reused
# across many preparations it became an invisible wall: a 0.2 CNY authorization stopped after
# four preparations while 0.187 CNY was still unspent (seen live on 2026-11-16). Planning keeps
# its own fuse at the same order as the goal-level training budget (48), and the money gate
# below stays the real limit.
MAX_PREPARATION_HTTP_ATTEMPTS = 48


class PreparationLedger(BillingLedger):
    """Reserve/settle against `app.model_http_calls` with `purpose='career_interview_prep'`."""

    def __init__(self, pool, policy, authorization_id, repository):
        super().__init__(pool, policy, authorization_id)
        self.repository = repository

    async def reserve(self, claim, context, payload_hash, payload_bytes):
        async with self.pool.connection() as conn, conn.transaction():
            auth = await (await conn.execute(
                'SELECT * FROM app.model_authorizations WHERE id=%s FOR UPDATE',
                (self.authorization_id,))).fetchone()
            if not auth or auth['policy_hash'] != self.policy.digest:
                raise BillingUnavailable('model_authorization_unavailable')
            if auth['state'] != 'ACTIVE':
                raise BillingUnavailable('model_usage_unknown')
            row = await self.repository._owned(conn, claim)
            if not row:
                raise BillingUnavailable('model_execution_expired')
            if digest(context) != digest(row['frozen'] or {}):
                raise BillingUnavailable('model_context_changed')
            prior = await (await conn.execute(
                'SELECT authorization_id,state,attempt FROM app.model_http_calls WHERE operation_id=%s',
                (claim.message.operation_id,))).fetchall()
            if any(item['authorization_id'] != self.authorization_id or item['state'] != 'KNOWN'
                   for item in prior):
                raise BillingUnavailable('model_usage_unknown')
            if len(prior) >= MAX_PREPARATION_REQUESTS:
                raise BillingUnavailable('model_budget_exhausted', 'preparation_request_limit')
            goal = await (await conn.execute(
                'SELECT * FROM app.career_goals WHERE id=%s', (row['goal_id'],))).fetchone()
            if legacy_budget_exhausted(goal) or goal['training_model_requests'] >= 48:
                raise BillingUnavailable('model_budget_exhausted', 'goal_training_budget')
            totals = await (await conn.execute(
                "SELECT count(*) AS n, COALESCE(sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END),0) AS used "
                'FROM app.model_http_calls WHERE authorization_id=%s', (self.authorization_id,))).fetchone()
            if (totals['n'] >= MAX_PREPARATION_HTTP_ATTEMPTS
                    or totals['used'] + self.policy.reservation_nano > auth['max_nano']):
                raise BillingUnavailable('model_budget_exhausted', 'authorization_http_limit')
            identity = str(uuid4())
            await conn.execute('''INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,
                fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,purpose)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (identity, self.authorization_id, claim.message.operation_id, claim.message.job_id,
                 claim.attempt, claim.fencing_token, digest(context), payload_hash, payload_bytes,
                 self.policy.reservation_nano, PURPOSE))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,'
                               'training_model_requests=training_model_requests+1,revision=revision+1 WHERE id=%s',
                               (goal['id'],))
            return identity
