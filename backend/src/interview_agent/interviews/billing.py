from interview_agent.career.budget import legacy_budget_exhausted
from typing import Literal
from uuid import uuid4
from interview_agent.billing.config import LivePolicy
from interview_agent.billing.ledger import BillingLedger,BillingUnavailable
from interview_agent.career.repository import digest
from .repository import InterviewRepository

AUTHORIZATION_ID='ja-05-20261028-cny2'


class InterviewPolicy(LivePolicy):
    profile:Literal['ja05-deepseek-flash']


class InterviewLedger(BillingLedger):
    async def reserve(self,claim,context,payload_hash,payload_bytes):
        async with self.pool.connection() as conn,conn.transaction():
            auth=await (await conn.execute('SELECT * FROM app.model_authorizations WHERE id=%s FOR UPDATE',(self.authorization_id,))).fetchone()
            if not auth or auth['policy_hash']!=self.policy.digest or self.policy.profile!='ja05-deepseek-flash':raise BillingUnavailable('model_authorization_unavailable')
            if auth['state']!='ACTIVE':raise BillingUnavailable('model_usage_unknown')
            repo=InterviewRepository(self.pool);rows=await repo._owned(conn,claim)
            if not rows:raise BillingUnavailable('model_execution_expired')
            root,s,r=rows
            if digest(context)!=digest(r['input_payload']):raise BillingUnavailable('model_context_changed')
            prior=await (await conn.execute('SELECT authorization_id,state,attempt FROM app.model_http_calls WHERE operation_id=%s',(r['id'],))).fetchall()
            if any(p['authorization_id']!=self.authorization_id or p['state']!='KNOWN' for p in prior):raise BillingUnavailable('model_usage_unknown')
            if any(p['attempt']!=claim.attempt for p in prior):raise BillingUnavailable('interview_resume_requires_new_input')
            total=await (await conn.execute("SELECT count(*) AS n,coalesce(sum(CASE WHEN state='KNOWN' THEN accounted_nano ELSE reserved_nano END),0) AS used FROM app.model_http_calls WHERE authorization_id=%s",(self.authorization_id,))).fetchone()
            if (total['n']>=self.policy.max_http_attempts or total['used']+self.policy.reservation_nano>auth['max_nano'] or legacy_budget_exhausted(root[0])
                or root[0]['training_model_requests']>=80 or s['model_requests']>=80 or r['model_requests']>=min(5,self.policy.max_operation_attempts)):
                raise BillingUnavailable('model_budget_exhausted')
            identity=str(uuid4())
            await conn.execute('''INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,purpose)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'career_interview')''',
                (identity,self.authorization_id,r['id'],r['job_id'],claim.attempt,claim.fencing_token,digest(context),payload_hash,payload_bytes,self.policy.reservation_nano))
            await repo._increment(conn,root[0],s,r);return identity
