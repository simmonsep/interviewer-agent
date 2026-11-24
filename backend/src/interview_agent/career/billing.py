"""Career-specific pre-HTTP admission; shared usage settlement preserves old books."""
from interview_agent.career.budget import legacy_budget_exhausted
from typing import Literal
from uuid import uuid4
from interview_agent.billing.config import LivePolicy
from interview_agent.billing.ledger import BillingLedger, BillingUnavailable
from .repository import CareerRepository,digest

AUTHORIZATION_ID='ja-01-20261027-cny2'


class CareerPolicy(LivePolicy):
    profile: Literal['ja01-deepseek-flash','ja03-deepseek-flash']


class CareerLedger(BillingLedger):
    async def reserve(self,claim,context,payload_hash,payload_bytes):
        async with self.pool.connection() as conn,conn.transaction():
            auth=await (await conn.execute('SELECT * FROM app.model_authorizations WHERE id=%s FOR UPDATE',(self.authorization_id,))).fetchone()
            if auth is None or auth['policy_hash']!=self.policy.digest: raise BillingUnavailable('model_authorization_unavailable')
            if auth['state']!='ACTIVE': raise BillingUnavailable('model_usage_unknown')
            repo=CareerRepository(self.pool)
            rows=await repo._owned(conn,claim)
            if rows is None: raise BillingUnavailable('model_execution_expired')
            goal,step=rows
            if context.conditions.auto_research and self.policy.profile!='ja03-deepseek-flash':raise BillingUnavailable('model_authorization_unavailable')
            if digest((await repo.context_at(conn,goal)).model_dump(mode='json'))!=digest(context.model_dump(mode='json')):
                raise BillingUnavailable('model_context_changed')
            previous=await (await conn.execute('SELECT authorization_id,state,attempt FROM app.model_http_calls WHERE operation_id=%s',(step['id'],))).fetchall()
            if any(p['authorization_id']!=self.authorization_id or p['state']!='KNOWN' for p in previous):
                raise BillingUnavailable('model_usage_unknown')
            if any(p['attempt']!=claim.attempt for p in previous):
                raise BillingUnavailable('career_resume_requires_new_input')
            total=await (await conn.execute("SELECT count(*) AS n,coalesce(sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END),0) AS used FROM app.model_http_calls WHERE authorization_id=%s",(self.authorization_id,))).fetchone()
            if (total['n']>=self.policy.max_http_attempts or total['used']+self.policy.reservation_nano>auth['max_nano']
                or goal['coordination_research_requests']>=12 or legacy_budget_exhausted(goal) or step['model_requests']>=min(3,self.policy.max_operation_attempts)):
                raise BillingUnavailable('model_budget_exhausted')
            identity=str(uuid4())
            await conn.execute('''INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,fencing_token,
                input_hash,payload_hash,payload_bytes,reserved_nano,purpose) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'career_coordination')''',
                (identity,self.authorization_id,step['id'],step['job_id'],claim.attempt,claim.fencing_token,
                 digest(context.model_dump(mode='json')),payload_hash,payload_bytes,self.policy.reservation_nano))
            await conn.execute('UPDATE app.career_steps SET model_requests=model_requests+1 WHERE id=%s',(step['id'],))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,coordination_research_requests=coordination_research_requests+1,revision=revision+1 WHERE id=%s',(goal['id'],))
            return identity
