"""SQL authority for written sessions, original code, execution leases and reports."""
import hashlib
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim, duration
from interview_agent.career.repository import digest
from interview_agent.materials.repository import MaterialRepository
from .catalog import task, public_task

RUNNING = ('PENDING','RUNNING')
VERDICTS = {'ACCEPTED','WRONG_ANSWER','COMPILE_ERROR','RUNTIME_ERROR','TIME_LIMIT','MEMORY_LIMIT',
            'OUTPUT_LIMIT','ENVIRONMENT_ERROR','CANCELLED'}


class WrittenRepository:
    def __init__(self, pool, image=None): self.pool = pool; self.image = image

    async def _root(self, conn, gid, user=None):
        goal = await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s'+(' AND user_id=%s' if user else '')+' FOR UPDATE',
                                         (gid,user) if user else (gid,))).fetchone()
        if not goal: raise ApiError('resource_unavailable',404)
        return goal

    async def _cancel_runs(self, conn, sid, reason):
        await conn.execute("""UPDATE app.career_written_submissions SET status='CANCELLED',lease_until=NULL,
            fencing_token=fencing_token+1,error_code=%s,result=%s WHERE session_id=%s AND status IN ('PENDING','RUNNING')""",
                           (reason,Jsonb({'verdict':'CANCELLED','code':reason,'cases':[]}),sid))

    async def _refresh(self, conn, goal, s):
        pending = await (await conn.execute("SELECT id FROM app.career_written_submissions WHERE session_id=%s AND status IN ('PENDING','RUNNING') LIMIT 1",(s['id'],))).fetchone()
        if s['status']=='ACTIVE' or pending:
            target = await MaterialRepository(self.pool)._target(conn,goal)
            if goal['status']=='CANCELLED' or goal['version']!=s['goal_version'] or (not s['input_payload'].get('application') and digest(target)!=digest(s['input_payload']['target'])):
                await conn.execute("UPDATE app.career_written_sessions SET status='STALE',stop_reason='context_changed',version=version+1,revision=revision+1 WHERE id=%s",(s['id'],))
                await self._cancel_runs(conn,s['id'],'context_changed')
            elif s['status']=='ACTIVE':
                await conn.execute("""UPDATE app.career_written_sessions SET status='COMPLETED',stop_reason='deadline',version=version+1,revision=revision+1
                    WHERE id=%s AND deadline_at<=clock_timestamp()""",(s['id'],))
        current=await (await conn.execute('SELECT * FROM app.career_written_sessions WHERE id=%s',(s['id'],))).fetchone()
        if current['input_payload'].get('kind')=='programming_validation' and current['status']=='ACTIVE' and not pending:
            final=await (await conn.execute('SELECT status FROM app.career_written_submissions WHERE session_id=%s ORDER BY seq DESC LIMIT 1',(s['id'],))).fetchone()
            if final and final['status'] not in RUNNING:
                current=await (await conn.execute("UPDATE app.career_written_sessions SET status='COMPLETED',stop_reason='reference_validation_complete',version=version+1,revision=revision+1 WHERE id=%s RETURNING *",(s['id'],))).fetchone()
        if current['input_payload'].get('kind')=='question_set':
            from .question_sets import ensure_report
            await ensure_report(conn,current)
        return current

    async def _session(self, conn, gid, sid, user=None):
        goal = await self._root(conn,gid,user)
        s = await (await conn.execute('SELECT * FROM app.career_written_sessions WHERE id=%s AND goal_id=%s FOR UPDATE',(sid,gid))).fetchone()
        if not s: raise ApiError('resource_unavailable',404)
        return goal, await self._refresh(conn,goal,s)

    async def create(self, principal, gid, key, value):
        if value.programming_task:
            from .programming import create
            return await create(self,principal,gid,key,value)
        if value.question_banks:
            from .question_sets import create
            return await create(self,principal,gid,key,value)
        if self.image is None: raise ApiError('written_executor_disabled',503)
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal); goal = await self._root(conn,gid,principal.user_id)
            from .contracts import creation_body
            body=creation_body(value)
            hashed = digest(body)
            prior = await (await conn.execute('SELECT id,request_hash FROM app.career_written_sessions WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed: raise ApiError('idempotency_conflict',409)
                return str(prior['id']),False
            if goal['version']!=value.expected_version or goal['status']=='CANCELLED': raise ApiError('version_conflict',409)
            old = await (await conn.execute('SELECT * FROM app.career_written_sessions WHERE goal_id=%s ORDER BY created_at FOR UPDATE',(gid,))).fetchall()
            if len(old)>=20: raise ApiError('written_session_limit',409)
            for s in old:
                current = await self._refresh(conn,goal,s)
                pending = await (await conn.execute("SELECT id FROM app.career_written_submissions WHERE session_id=%s AND status IN ('PENDING','RUNNING') LIMIT 1",(s['id'],))).fetchone()
                if current['status']=='ACTIVE' or pending: raise ApiError('written_in_progress',409)
            if goal['written_submissions']>=12: raise ApiError('written_execution_budget_exhausted',409)
            chosen = value.task_id or 'idempotency-record'; basis = {'origin':'user_selection' if value.task_id else 'default_backend_task'}
            if value.task_id is None:
                # F07Z-74: a retracted observation must not keep driving the retest. The retraction
                # route writes app.career_written_retractions, but this query only filtered interview
                # corrections, so withdrawing an observation changed nothing.
                observation = await (await conn.execute("""SELECT o.id,o.payload FROM app.career_interview_observations o
                    JOIN app.career_interviews i ON i.id=o.session_id WHERE o.goal_id=%s AND i.status='COMPLETED'
                    AND o.payload->>'topic_id'='redis-lock' AND o.payload->>'label'='needs_probe'
                    AND NOT EXISTS(SELECT 1 FROM app.career_interview_corrections c WHERE c.observation_id=o.id)
                    AND NOT EXISTS(SELECT 1 FROM app.career_written_retractions r WHERE r.observation_id=o.id)
                    ORDER BY o.created_at DESC LIMIT 1""",(gid,))).fetchone()
                if observation:
                    chosen='lock-owner-expiry'; basis={'origin':'deterministic_retest_rule','observation_id':str(observation['id']),
                                                     'claim':'待复测假设，不是能力结论'}
            payload={'task':task(chosen),'target':await MaterialRepository(self.pool)._target(conn,goal),'selection_basis':basis,
                     'policy':{'version':'ja06-independent-v1','mode':'independent','system_ai_help':False,'image':self.image,
                               'max_submissions':3,'language':'java21','duration_seconds':value.duration_seconds,
                               'execution_policy':'ja06-docker-java-v1','external_assistance_verified':False}}
            from .context import enrich
            await enrich(conn,self,gid,value,payload)
            s=await (await conn.execute('''INSERT INTO app.career_written_sessions(goal_id,goal_version,request_id,request_hash,input_payload,deadline_at)
                VALUES(%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING id''',
                                       (gid,goal['version'],key,hashed,Jsonb(payload),value.duration_seconds))).fetchone()
            return str(s['id']),True

    async def submit(self, principal, gid, sid, key, value):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal); goal,s = await self._session(conn,gid,sid,principal.user_id)
            if s['input_payload'].get('kind')=='question_set':raise ApiError('written_code_session_required',400)
            if s['input_payload'].get('kind')=='programming_validation':raise ApiError('reference_validation_is_immutable',400)
            hashed=digest(value.model_dump(mode='json'))
            prior=await (await conn.execute('SELECT id,request_hash FROM app.career_written_submissions WHERE session_id=%s AND request_id=%s',(sid,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed: raise ApiError('idempotency_conflict',409)
                return str(prior['id']),False
            if s['status']!='ACTIVE' or s['version']!=value.expected_version: raise ApiError('version_conflict',409)
            if s['submissions']>=3 or goal['written_submissions']>=12: raise ApiError('written_execution_budget_exhausted',409)
            if await (await conn.execute("SELECT id FROM app.career_written_submissions WHERE session_id=%s AND status IN ('PENDING','RUNNING')",(sid,))).fetchone():
                raise ApiError('written_execution_in_progress',409)
            row=await (await conn.execute('''INSERT INTO app.career_written_submissions(session_id,goal_id,goal_version,seq,request_id,request_hash,source,source_sha256,input_payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (sid,gid,goal['version'],s['submissions']+1,key,hashed,value.source,hashlib.sha256(value.source.encode()).hexdigest(),Jsonb(s['input_payload'])))).fetchone()
            await conn.execute('UPDATE app.career_written_sessions SET submissions=submissions+1,version=version+1,revision=revision+1 WHERE id=%s',(sid,))
            await conn.execute('UPDATE app.career_goals SET written_submissions=written_submissions+1,revision=revision+1 WHERE id=%s',(gid,))
            return str(row['id']),True

    async def end(self, principal, gid, sid, value, *, cancel=False):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal); goal,s=await self._session(conn,gid,sid,principal.user_id)
            if s['version']!=value.expected_version: raise ApiError('version_conflict',409)
            if s['status']=='STALE': raise ApiError('version_conflict',409)
            if s['input_payload'].get('kind')=='question_set' and s['status']!='ACTIVE':return
            if cancel:
                await self._cancel_runs(conn,sid,'user_cancel')
            if s['status']=='ACTIVE' or cancel:
                await conn.execute('UPDATE app.career_written_sessions SET status=%s,stop_reason=%s,version=version+1,revision=revision+1 WHERE id=%s',
                                   ('CANCELLED' if cancel else 'COMPLETED','user_cancel' if cancel else 'user_finish',sid))
                if s['input_payload'].get('kind')=='question_set':
                    from .question_sets import ensure_report
                    current=await (await conn.execute('SELECT * FROM app.career_written_sessions WHERE id=%s',(sid,))).fetchone()
                    await ensure_report(conn,current)

    def _visible_result(self, result, ended):
        if result is None: return None
        if ended: return result
        return {'public_cases':[x for x in result.get('cases',[]) if x['visibility']=='public'],
                'execution_error':result['verdict'] if result['verdict'] in ('COMPILE_ERROR','ENVIRONMENT_ERROR','CANCELLED') else None,
                'diagnostic':result.get('diagnostic') if result['verdict']=='COMPILE_ERROR' else None}

    async def get(self, principal, gid, sid):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal); goal,s=await self._session(conn,gid,sid,principal.user_id)
            if s['input_payload'].get('kind')=='question_set':
                from .question_sets import view
                return await view(conn,s)
            submissions=await (await conn.execute('SELECT id,seq,source,source_sha256,status,result,error_code,attempt,created_at,execution_started FROM app.career_written_submissions WHERE session_id=%s ORDER BY seq',(sid,))).fetchall()
            ended=s['status']!='ACTIVE'; t=s['input_payload']['task']
            return {'id':str(s['id']),'goal_version':s['goal_version'],'version':s['version'],'revision':s['revision'],'status':s['status'],
                    'deadline_at':s['deadline_at'],'stop_reason':s['stop_reason'],'policy':s['input_payload']['policy'],
                    'target':s['input_payload']['target'],'application':s['input_payload'].get('application'),'retest_observation_id':(s['input_payload'].get('retest_source') or {}).get('observation_id'),'task':public_task(t),'selection_basis':s['input_payload']['selection_basis'],
                    'kind':s['input_payload'].get('kind','builtin_programming'),
                    'task_provenance':t.get('bank_item'),
                    'validation':{'passed':s['status']=='COMPLETED' and any(r['status']=='SUCCEEDED' and r['execution_started'] and (r['result'] or {}).get('verdict')=='ACCEPTED' for r in submissions),
                                  'qualification':'reference_passed_declared_cases_not_task_quality_review','image':s['input_payload']['policy']['image']} if s['input_payload'].get('kind')=='programming_validation' else None,
                    'submissions':[{**{k:r[k] for k in ('seq','source','source_sha256','status','error_code','attempt','created_at')},
                                    'id':str(r['id']),'result':self._visible_result(r['result'],ended)} for r in submissions],
                    'report':{'analysis':t['analysis'],'graded_submissions':sum(r['status']=='SUCCEEDED' for r in submissions),
                              'pending_submissions':sum(r['status'] in RUNNING for r in submissions),
                              'next_action':'对照原提交和错误用例类别解释状态转换，再用另一道题复测。',
                              'ability_certified':False,'external_assistance_verified':False} if ended else None}

    async def listing(self, principal, gid):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal); await self._root(conn,gid,principal.user_id)
            rows=await (await conn.execute('SELECT id FROM app.career_written_sessions WHERE goal_id=%s ORDER BY created_at DESC LIMIT 20',(gid,))).fetchall()
        return [await self.get(principal,gid,str(r['id'])) for r in rows]

    async def _lock(self, conn, message):
        ref=await (await conn.execute('SELECT goal_id,session_id FROM app.career_written_submissions WHERE id=%s AND job_id=%s',(message.operation_id,message.job_id))).fetchone()
        if not ref: return None
        goal,s=await self._session(conn,ref['goal_id'],ref['session_id'])
        r=await (await conn.execute('SELECT * FROM app.career_written_submissions WHERE id=%s FOR UPDATE',(message.operation_id,))).fetchone()
        return goal,s,r

    async def _owned(self, conn, claim):
        rows=await self._lock(conn,claim.message)
        if not rows: return None
        g,s,r=rows; now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (s['status'] not in ('ACTIVE','COMPLETED') or r['status']!='RUNNING' or r['attempt']!=claim.attempt
            or r['fencing_token']!=claim.fencing_token or r['lease_until']<=now or r['goal_version']!=claim.message.resource_version
            or str(g['user_id'])!=claim.user_id or str(g['id'])!=claim.resource_id): return None
        return rows

    async def pending_dispatch(self, recovery_seconds=5):
        async with self.pool.connection() as conn:
            sessions=await (await conn.execute("SELECT id,goal_id FROM app.career_written_sessions WHERE status='ACTIVE' OR EXISTS(SELECT 1 FROM app.career_written_submissions r WHERE r.session_id=career_written_sessions.id AND r.status IN ('PENDING','RUNNING')) ORDER BY created_at LIMIT 100")).fetchall()
            for s in sessions:
                async with conn.transaction(): await self._session(conn,s['goal_id'],s['id'])
            return await (await conn.execute("""SELECT id AS outbox_id,id AS operation_id,job_id,goal_version AS resource_version FROM app.career_written_submissions
                WHERE (status='PENDING' OR (status='RUNNING' AND lease_until<=clock_timestamp()))
                AND (dispatched_at IS NULL OR dispatched_at<=clock_timestamp()-(%s*interval '1 second')) ORDER BY created_at LIMIT 100""",(recovery_seconds,))).fetchall()

    async def mark_dispatched(self, identity):
        async with self.pool.connection() as conn: await conn.execute('UPDATE app.career_written_submissions SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s',(identity,))

    async def claim(self, message, lease_seconds=6):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            if not rows: return None
            g,s,r=rows; now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if (s['status'] not in ('ACTIVE','COMPLETED') or r['status'] not in RUNNING
                or r['goal_version']!=message.resource_version or (r['status']=='RUNNING' and r['lease_until']>now)): return None
            if r['attempt']>=3 or (r['execution_started'] and r['candidate'] is None):
                await conn.execute("UPDATE app.career_written_submissions SET status='FAILED',lease_until=NULL,error_code='execution_outcome_unknown',result=%s WHERE id=%s",
                                   (Jsonb({'verdict':'ENVIRONMENT_ERROR','code':'execution_outcome_unknown','cases':[]}),r['id']))
                return None
            row=await (await conn.execute("UPDATE app.career_written_submissions SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s RETURNING *",(duration(lease_seconds),r['id']))).fetchone()
            return Claim(message,str(g['user_id']),str(g['id']),row['attempt'],row['fencing_token'],'',kind='written_execution',session_id=str(s['id']))

    async def heartbeat(self, claim, lease_seconds=6):
        async with self.pool.connection() as conn,conn.transaction():
            if not await self._owned(conn,claim): return False
            await conn.execute("UPDATE app.career_written_submissions SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",(duration(lease_seconds),claim.message.operation_id)); return True

    async def context(self, claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows: raise ValueError('written_claim_expired')
            return rows[2]

    async def begin(self, claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows or rows[2]['execution_started']: return False
            await conn.execute('UPDATE app.career_written_submissions SET execution_started=true WHERE id=%s',(claim.message.operation_id,)); return True

    def validate_result(self, row, result):
        if not isinstance(result,dict) or result.get('verdict') not in VERDICTS: raise ValueError('invalid_execution_result')
        if result.get('verdict') not in ('ENVIRONMENT_ERROR','CANCELLED'):
            if result.get('evidence',{}).get('source_sha256')!=row['source_sha256']: raise ValueError('execution_source_mismatch')
            if result.get('evidence',{}).get('policy')!=row['input_payload']['policy']['execution_policy']: raise ValueError('execution_policy_mismatch')
            if result.get('evidence',{}).get('image')!=row['input_payload']['policy']['image']: raise ValueError('execution_image_mismatch')
        expected={x['id']:x for x in row['input_payload']['task']['cases']}; seen=set()
        for c in result.get('cases',[]):
            if c['id'] not in expected or c['id'] in seen or c.get('visibility')!=expected[c['id']]['visibility'] or c.get('verdict') not in VERDICTS:
                raise ValueError('invalid_case_result')
            seen.add(c['id'])
        if result['verdict']=='ACCEPTED' and (seen!=set(expected) or any(c['verdict']!='ACCEPTED' for c in result['cases'])):
            raise ValueError('incomplete_acceptance')
        return result

    async def save(self, claim, result):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows: return False
            checked=self.validate_result(rows[2],result)
            await conn.execute('UPDATE app.career_written_submissions SET candidate=%s WHERE id=%s',(Jsonb(checked),claim.message.operation_id)); return True

    async def complete(self, claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows: return False
            g,s,r=rows; result=self.validate_result(r,r['candidate'])
            status='FAILED' if result['verdict']=='ENVIRONMENT_ERROR' else 'CANCELLED' if result['verdict']=='CANCELLED' else 'SUCCEEDED'
            await conn.execute('UPDATE app.career_written_submissions SET status=%s,lease_until=NULL,result=%s,error_code=%s WHERE id=%s',
                               (status,Jsonb(result),result.get('code') if status!='SUCCEEDED' else None,r['id']))
            if s['input_payload'].get('kind')=='programming_validation':
                await conn.execute("UPDATE app.career_written_sessions SET status='COMPLETED',stop_reason='reference_validation_complete',version=version+1,revision=revision+1 WHERE id=%s",(s['id'],))
            else:await conn.execute('UPDATE app.career_written_sessions SET revision=revision+1 WHERE id=%s',(s['id'],))
            return True

    async def fail_attempt(self, claim):
        result={'verdict':'ENVIRONMENT_ERROR','code':'written_executor_failed','cases':[]}
        return await self.save(claim,result) and await self.complete(claim)

    async def can_ack(self, message):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            return not rows or rows[2]['status'] not in RUNNING
