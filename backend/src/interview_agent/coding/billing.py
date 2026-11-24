"""HTTP-level coding authorization; no model call occurs in this module's CLI."""
import argparse
import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4
from pydantic import Field
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.billing.config import LivePolicy
from interview_agent.billing.ledger import BillingLedger, BillingUnavailable, authorize
from interview_agent.storage.config import DatabaseSettings
from interview_agent.career.repository import digest
from .repository import CodingRepository

AUTHORIZATION_ID = 'ja-07-03-20261029-cny2'


class CodingPolicy(LivePolicy):
    profile: Literal['ja07-deepseek-flash']
    max_http_attempts: Annotated[int, Field(ge=1, le=24)]
    max_operation_attempts: Annotated[int, Field(ge=1, le=12)]


class CodingLedger(BillingLedger):
    async def reserve(self, claim, context, payload_hash, payload_bytes):
        async with self.pool.connection() as conn, conn.transaction():
            auth = await (await conn.execute('SELECT * FROM app.model_authorizations WHERE id=%s FOR UPDATE', (self.authorization_id,))).fetchone()
            if not auth or auth['policy_hash'] != self.policy.digest or self.policy.profile != 'ja07-deepseek-flash':
                raise BillingUnavailable('model_authorization_unavailable')
            if auth['state'] != 'ACTIVE': raise BillingUnavailable('model_usage_unknown')
            rows = await CodingRepository(self.pool)._owned(conn, claim)
            if not rows: raise BillingUnavailable('model_execution_expired')
            g, s, r = rows
            if (r['kind'] != 'assist' or r['origin'] != 'real_model' or r['input_payload']['policy'].get('assistant_provider') != 'live'
                    or digest(context) != digest(r['input_payload'])): raise BillingUnavailable('model_context_changed')
            prior = await (await conn.execute('SELECT authorization_id,state,attempt FROM app.model_http_calls WHERE operation_id=%s', (r['id'],))).fetchall()
            if any(x['authorization_id'] != self.authorization_id or x['state'] != 'KNOWN' for x in prior): raise BillingUnavailable('model_usage_unknown')
            if any(x['attempt'] != claim.attempt for x in prior): raise BillingUnavailable('coding_resume_requires_new_input')
            totals = await (await conn.execute("SELECT count(*) AS n,coalesce(sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END),0) AS used FROM app.model_http_calls WHERE authorization_id=%s", (self.authorization_id,))).fetchone()
            if (totals['n'] >= self.policy.max_http_attempts or totals['used'] + self.policy.reservation_nano > auth['max_nano']
                    or g['model_requests'] >= 120 or g['coding_model_requests'] >= 48 or s['model_requests'] >= 24
                    or r['model_requests'] >= min(12, self.policy.max_operation_attempts)):
                raise BillingUnavailable('model_budget_exhausted')
            identity = str(uuid4())
            await conn.execute('''INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,purpose)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'career_coding')''',
                (identity, self.authorization_id, r['id'], r['job_id'], claim.attempt, claim.fencing_token, digest(context), payload_hash, payload_bytes, self.policy.reservation_nano))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,coding_model_requests=coding_model_requests+1,revision=revision+1 WHERE id=%s', (g['id'],))
            await conn.execute('UPDATE app.career_coding_sessions SET model_requests=model_requests+1,revision=revision+1 WHERE id=%s', (s['id'],))
            await conn.execute('UPDATE app.career_coding_runs SET model_requests=model_requests+1 WHERE id=%s', (r['id'],))
            return identity


async def run(args):
    policy = CodingPolicy.load(args.policy)
    raw = json.loads(args.database.read_text(encoding='utf-8'))
    db = DatabaseSettings.model_validate(raw.get('database', raw))
    if args.authorization == AUTHORIZATION_ID and db.dbname != 'py_interview': raise ValueError('live_authorization_requires_development_database')
    if args.action == 'authorize':
        await authorize(db, policy, args.authorization); print(json.dumps({'authorization': args.authorization, 'max_cny': 2, 'requests_sent': 0})); return
    pool = AsyncConnectionPool(db.conninfo(), open=False, kwargs={'row_factory': dict_row})
    try:
        await pool.open(wait=True); print(json.dumps(await CodingLedger(pool, policy, args.authorization).summary(), ensure_ascii=False))
    finally: await pool.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('action', choices=['authorize', 'summary'])
    p.add_argument('--database', type=Path, required=True); p.add_argument('--policy', type=Path, default=Path('configs/ja07-deepseek-flash.json'))
    p.add_argument('--authorization', default=AUTHORIZATION_ID)
    asyncio.run(run(p.parse_args()), loop_factory=selector_loop)
