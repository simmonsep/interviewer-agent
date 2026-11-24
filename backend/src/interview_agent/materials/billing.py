"""JA-04 admission: one authorization across analysis and drafts; no old authorization reuse."""
from interview_agent.career.budget import NON_CODING_REQUEST_ALLOCATION, legacy_budget_exhausted
from typing import Literal
from uuid import uuid4
from interview_agent.billing.config import LivePolicy
from interview_agent.billing.ledger import BillingLedger,BillingUnavailable
from interview_agent.career.repository import digest
from .repository import MaterialRepository
from .segments import MAX_REQUESTS

AUTHORIZATION_ID='ja-04-20261028-cny2'


class MaterialPolicy(LivePolicy):
    profile: Literal['ja04-deepseek-flash']


# F07Z-112 (decision-maker ruling on F07Z-111): the frozen desktop has no configs/ directory,
# so the material policy must exist in code, exactly like the interview default. The digest is
# pinned to configs/ja04-deepseek-flash.json by a test; the two policies stay independent
# because the ledger refuses an authorization whose policy_hash does not match.
DEFAULT_MATERIAL_POLICY = {
    'profile': 'ja04-deepseek-flash',
    'model': 'deepseek-flash',
    'observed_model_version': 'DeepSeek-V4.1-Flash-1017',
    'endpoint': 'https://api.deepseek.com/chat/completions',
    'thinking': 'disabled',
    'max_output_tokens': 2048,
    'max_payload_bytes': 32768,
    'input_reservation_tokens': 65536,
    'max_http_attempts': 12,
    'max_operation_attempts': 12,
    'timeout_seconds': 45,
    'input_miss_nano_cny_per_token': 2000,
    'input_hit_nano_cny_per_token': 40,
    'output_nano_cny_per_token': 8000,
    'pricing_checked_at': '2026-10-28',
    'pricing_basis': 'Published CNY peak rates conservatively bound JA-04 analysis and draft calls under one CNY2 authorization; not provider invoice',
    'official_sources': ['https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'],
}


def default_material_policy():
    """The material policy used when the desktop has no configs/ directory to load."""
    return MaterialPolicy.model_validate(DEFAULT_MATERIAL_POLICY)



class MaterialLedger(BillingLedger):
    async def reserve(self,claim,context,payload_hash,payload_bytes):
        async with self.pool.connection() as conn,conn.transaction():
            auth=await (await conn.execute('SELECT * FROM app.model_authorizations WHERE id=%s FOR UPDATE',(self.authorization_id,))).fetchone()
            if auth is None or auth['policy_hash']!=self.policy.digest or self.policy.profile!='ja04-deepseek-flash':raise BillingUnavailable('model_authorization_unavailable')
            if auth['state']!='ACTIVE':raise BillingUnavailable('model_usage_unknown')
            rows=await MaterialRepository(self.pool)._owned(conn,claim)
            if not rows:raise BillingUnavailable('model_execution_expired')
            goal,state,run=rows
            if digest(run['input_payload'])!=digest(context):raise BillingUnavailable('model_context_changed')
            prior=await (await conn.execute('SELECT authorization_id,state,attempt FROM app.model_http_calls WHERE operation_id=%s',(run['id'],))).fetchall()
            if any(x['authorization_id']!=self.authorization_id or x['state']!='KNOWN' for x in prior):raise BillingUnavailable('model_usage_unknown')
            if any(x['attempt']!=claim.attempt for x in prior):raise BillingUnavailable('material_resume_requires_new_input')
            total=await (await conn.execute("SELECT count(*) AS n,coalesce(sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END),0) AS used FROM app.model_http_calls WHERE authorization_id=%s",(self.authorization_id,))).fetchone()
            # F07Z-120 (decision THIRD_OPTION): the four counters have four different jobs and
            # this one was doing the wrong one. `max_http_attempts` is the *per-run* ceiling
            # (`segments.request_ceiling`); the desktop reuses one authorization row for every
            # run at the same cap (`model_settings/routes.py` ON CONFLICT DO NOTHING), so
            # counting the row's whole history against it made each cap value a 12-call
            # lifetime quota. Run-level and goal-level fuses are checked below; the money
            # check is the user's total. Nothing is lost by dropping this guard here.
            if total['used']+self.policy.reservation_nano>auth['max_nano']:
                raise BillingUnavailable('model_budget_exhausted',limit='authorization_nano_limit')
            if legacy_budget_exhausted(goal):
                raise BillingUnavailable('model_budget_exhausted',limit='goal_budget_limit')
            if goal['material_model_requests']>=NON_CODING_REQUEST_ALLOCATION:
                raise BillingUnavailable('model_budget_exhausted',limit='goal_material_request_limit')
            if run['model_requests']>=min(MAX_REQUESTS,self.policy.max_operation_attempts):
                raise BillingUnavailable('model_budget_exhausted',limit='run_operation_attempt_limit')
            identity=str(uuid4())
            await conn.execute('''INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,purpose)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'career_materials')''',
                (identity,self.authorization_id,run['id'],run['job_id'],claim.attempt,claim.fencing_token,digest(context),payload_hash,payload_bytes,self.policy.reservation_nano))
            await conn.execute('UPDATE app.career_material_runs SET model_requests=model_requests+1 WHERE id=%s',(run['id'],))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,material_model_requests=material_model_requests+1,revision=revision+1 WHERE id=%s',(goal['id'],))
            return identity
