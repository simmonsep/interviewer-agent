"""Career steps are durable jobs + transactional outbox; SQL fences all commits."""
from interview_agent.career.budget import legacy_budget_exhausted
import hashlib
from psycopg.types.json import Jsonb
from interview_agent.contracts.operations import QueueMessage
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim, duration
from interview_agent.runtime.store import canonical
from .contracts import GoalContext,ApplicationPlan, validate_decision,request_body


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class CareerRepository:
    def __init__(self,pool): self.pool=pool

    async def create(self,principal,key,value):
        hashed=digest(request_body(value))
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn,principal)
            row=await (await conn.execute('''INSERT INTO app.career_goals(user_id,request_id,request_hash,conditions,jd_text)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,request_id) DO NOTHING RETURNING *''',
                (principal.user_id,key,hashed,Jsonb(value.conditions.model_dump()),value.jd_text))).fetchone()
            if row is None:
                prior=await (await conn.execute('SELECT id,request_hash FROM app.career_goals WHERE user_id=%s AND request_id=%s',
                    (principal.user_id,key))).fetchone()
                if prior['request_hash']!=hashed: raise ApiError('idempotency_conflict',409)
                return str(prior['id']),False
            await conn.execute('''INSERT INTO app.career_steps(goal_id,goal_version,request_id,request_hash,message,input_payload)
                VALUES(%s,1,%s,%s,%s,%s)''',(row['id'],key,hashed,value.message,Jsonb(value.model_dump(mode='json'))))
            return str(row['id']),True

    async def _goal(self,conn,user,identity,lock=False):
        row=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),
            (identity,user))).fetchone()
        if row is None: raise ApiError('resource_unavailable',404)
        return row

    async def get(self,principal,identity):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn,principal)
            goal=await self._goal(conn,principal.user_id,identity,True)
            rows=await (await conn.execute('''SELECT id,goal_version,cycle,message,status,error_code,origin,model_requests,tool_log
                FROM app.career_steps WHERE goal_id=%s ORDER BY goal_version,cycle''',(identity,))).fetchall()
            action=goal['action'];plan_state=None
            if action and action.get('application'):
                from interview_agent.applications.repository import ApplicationRepository
                c=action['application'];view=await ApplicationRepository(self.pool).coordinator_context_at(conn,goal,c['application_id'])
                plan_state='current' if view['context_sha256']==c['context_sha256'] and c['action_id'] in {a['id'] for a in view['actions']} else 'outdated'
                if plan_state=='outdated':action={**action,'text':'申请或训练证据已更新，请重新请求复盘；原提议仅作历史记录。'}
            return {**{k:goal[k] for k in ('version','revision','status','conditions','jd_text','profile','model_requests')},
                    'action':action,'application_plan_state':plan_state,
                    'coordination_cycle':rows[-1]['cycle'] if rows else 0,'continuation_limit':3,
                    'id':str(goal['id']),'steps':[{**dict(r),'id':str(r['id'])} for r in rows]}

    async def list(self,principal):
        async with self.pool.connection() as conn:
            await require_session(conn,principal)
            rows=await (await conn.execute('SELECT id FROM app.career_goals WHERE user_id=%s ORDER BY created_at DESC,id LIMIT 30',
                (principal.user_id,))).fetchall()
        return [str(r['id']) for r in rows]

    async def workflow(self,principal,identity):
        from .handoffs import feedback
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await self._goal(conn,principal.user_id,identity,True)
            return {'goal_version':goal['version'],**await feedback(conn,goal['id'],self.pool)}

    async def reply(self,principal,identity,key,value):
        hashed=digest(request_body(value))
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn,principal)
            goal=await self._goal(conn,principal.user_id,identity,True)
            prior=await (await conn.execute('SELECT request_hash FROM app.career_steps WHERE goal_id=%s AND request_id=%s',
                (identity,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed: raise ApiError('idempotency_conflict',409)
                return
            if goal['version']!=value.expected_version: raise ApiError('version_conflict',409)
            if goal['status']=='CANCELLED': raise ApiError('goal_cancelled',409)
            if goal['version']>=12: raise ApiError('goal_turn_limit',409)
            await conn.execute("UPDATE app.career_steps SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1 WHERE goal_id=%s AND status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')",(identity,))
            await conn.execute("""UPDATE app.career_goals SET version=version+1,revision=revision+1,status='PENDING',
                conditions=%s,jd_text=%s,profile=NULL,action=NULL,updated_at=clock_timestamp() WHERE id=%s""",
                (Jsonb(value.conditions.model_dump() if value.conditions is not None else goal['conditions']),
                 value.jd_text if value.jd_text is not None else goal['jd_text'],identity))
            await conn.execute('''INSERT INTO app.career_steps(goal_id,goal_version,request_id,request_hash,message,input_payload)
                VALUES(%s,%s,%s,%s,%s,%s)''',(identity,goal['version']+1,key,hashed,value.message,Jsonb(value.model_dump(mode='json'))))
            await self._carry_selection(conn,goal,goal['version']+1,
                value.jd_text if value.jd_text is not None else goal['jd_text'],
                value.conditions.model_dump(mode='json') if value.conditions is not None else goal['conditions'])

    async def _carry_selection(self,conn,goal,new_version,new_jd,new_conditions):
        """F07Z-166 D1: materialise the previous version's selection for the new version.

        Only the same, unchanged context may be carried: JD and conditions identical, the
        selected position still points at the selected snapshot, and no hard conflict or hard
        unknown other than the source's structural `availability` row. The allowance is
        deliberately explicit (field+source_path) so a future matching change cannot widen it.
        """
        from .matching import checks
        previous=goal['version']
        if (new_jd or '')!=(goal['jd_text'] or ''):
            return
        if canonical(new_conditions)!=(canonical(goal['conditions']) if goal['conditions'] else '{}'):
            return
        source=await (await conn.execute('''SELECT run_id,snapshot_id,acknowledged_unknown
            FROM app.career_job_selections WHERE goal_id=%s AND goal_version=%s''',
            (goal['id'],previous))).fetchone()
        if not source:return
        snapshot=await (await conn.execute('''SELECT s.payload,s.position_id,p.current_snapshot_id
            FROM app.career_job_snapshots s LEFT JOIN app.career_positions p ON p.id=s.position_id
            WHERE s.id=%s''',(source['snapshot_id'],))).fetchone()
        if (not snapshot or not snapshot['position_id']
                or snapshot['current_snapshot_id']!=source['snapshot_id']):
            return

        def structural(row):
            return row.get('field')=='availability' and row.get('source_path')=='availability'

        verdict=checks(snapshot['payload'],new_conditions,new_version)
        blocking=[row for row in verdict['checks']
                  if not structural(row) and row['hard'] and row['status'] in ('conflict','unknown')]
        if blocking:return
        await conn.execute('''INSERT INTO app.career_job_selections(goal_id,goal_version,run_id,snapshot_id,
            acknowledged_unknown,carried_from_version) VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(goal_id,goal_version) DO NOTHING''',
            (goal['id'],new_version,source['run_id'],source['snapshot_id'],
             source['acknowledged_unknown'],previous))

    async def cancel(self,principal,identity,version):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn,principal)
            goal=await self._goal(conn,principal.user_id,identity,True)
            if goal['version']!=version: raise ApiError('version_conflict',409)
            await conn.execute("UPDATE app.career_steps SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1 WHERE goal_id=%s AND status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')",(identity,))
            await conn.execute("UPDATE app.career_goals SET status='CANCELLED',revision=revision+1 WHERE id=%s",(identity,))

    async def pending_dispatch(self,recovery_seconds=5):
        async with self.pool.connection() as conn:
            return await (await conn.execute('''SELECT s.id AS outbox_id,s.id AS operation_id,s.job_id,s.goal_version AS resource_version
                FROM app.career_steps s JOIN app.career_goals g ON g.id=s.goal_id AND g.version=s.goal_version
                WHERE (s.status='PENDING' OR (s.status='RUNNING' AND s.lease_until<=clock_timestamp()))
                AND (s.dispatched_at IS NULL OR s.dispatched_at<=clock_timestamp()-(%s*interval '1 second'))
                ORDER BY s.created_at LIMIT 100''',(recovery_seconds,))).fetchall()

    async def mark_dispatched(self,identity):
        async with self.pool.connection() as conn:
            await conn.execute('UPDATE app.career_steps SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s',(identity,))

    async def _lock(self,conn,message):
        ref=await (await conn.execute('SELECT goal_id FROM app.career_steps WHERE id=%s AND job_id=%s',
            (message.operation_id,message.job_id))).fetchone()
        if ref is None: return None
        goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(ref['goal_id'],))).fetchone()
        step=await (await conn.execute('SELECT * FROM app.career_steps WHERE id=%s FOR UPDATE',(message.operation_id,))).fetchone()
        return (goal,step) if goal and step else None

    async def _owned(self,conn,claim):
        rows=await self._lock(conn,claim.message)
        if rows is None: return None
        goal,step=rows
        now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (step['status']!='RUNNING' or step['attempt']!=claim.attempt or step['fencing_token']!=claim.fencing_token
            or step['lease_until']<=now or goal['version']!=step['goal_version'] or goal['status']!='RUNNING'
            or str(goal['user_id'])!=claim.user_id or str(goal['id'])!=claim.resource_id
            or goal['version']!=claim.message.resource_version): return None
        return goal,step

    async def claim(self,message,lease_seconds=30):
        seconds=duration(lease_seconds)
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            if rows is None: return None
            goal,step=rows
            now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if (step['goal_version']!=message.resource_version or goal['version']!=message.resource_version
                or goal['status']=='CANCELLED' or step['status'] not in ('PENDING','RUNNING')): return None
            if step['status']=='RUNNING' and step['lease_until']>now: return None
            if step['attempt']>=3:
                await conn.execute("UPDATE app.career_steps SET status='FAILED',lease_until=NULL,error_code='attempt_limit' WHERE id=%s",(step['id'],))
                await conn.execute("UPDATE app.career_goals SET status='FAILED',revision=revision+1 WHERE id=%s",(goal['id'],))
                return None
            row=await (await conn.execute("""UPDATE app.career_steps SET status='RUNNING',attempt=attempt+1,
                fencing_token=fencing_token+1,lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s RETURNING *""",
                (seconds,step['id']))).fetchone()
            await conn.execute("UPDATE app.career_goals SET status='RUNNING',revision=revision+1 WHERE id=%s",(goal['id'],))
            return Claim(message,str(goal['user_id']),str(goal['id']),row['attempt'],row['fencing_token'],'',kind='coordinate_goal')

    async def heartbeat(self,claim,lease_seconds=30):
        async with self.pool.connection() as conn,conn.transaction():
            if await self._owned(conn,claim) is None: return False
            await conn.execute("UPDATE app.career_steps SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",
                (duration(lease_seconds),claim.message.operation_id))
            return True

    async def context_at(self,conn,goal):
        from interview_agent.applications.repository import ApplicationRepository
        from .handoffs import feedback as workflow_feedback
        messages=await (await conn.execute('SELECT id,message AS text,goal_version,cycle,status,candidate,input_payload FROM app.career_steps WHERE goal_id=%s ORDER BY goal_version,cycle',
            (goal['id'],))).fetchall()
        feedback=await (await conn.execute("SELECT goal_version,result FROM app.career_research_runs WHERE goal_id=%s AND mode='agent' AND result IS NOT NULL ORDER BY created_at DESC LIMIT 2",(goal['id'],))).fetchall()
        return GoalContext(goal_id=str(goal['id']),user_id=str(goal['user_id']),version=goal['version'],
            intent=next((r['input_payload'].get('intent') or 'auto' for r in reversed(messages) if r['input_payload'].get('origin','user')=='user'),'auto'),
            messages=[{'id':str(r['id']),'text':r['text'],'origin':r['input_payload'].get('origin','user')} for r in messages],conditions=goal['conditions'],jd_text=goal['jd_text'],
            previous_actions=[r['candidate'] for r in messages if r['status']=='SUCCEEDED' and r['candidate'] is not None
                              and (r['goal_version'],r['cycle'])<(goal['version'],messages[-1]['cycle'])],
            research_feedback=[{'goal_version':r['goal_version'],'readable':r['result']['readable'],'question':(r['result'].get('agent') or {}).get('question'),
                                'queries':(r['result'].get('agent') or {}).get('query_observations',[]),
                                'outcomes':[m['outcome'] for m in r['result'].get('matches',[])]} for r in feedback],
            applications=await ApplicationRepository(self.pool).summaries_at(conn,goal['id']),
            workflow_feedback=await workflow_feedback(conn,goal['id'],self.pool),
            completion_trigger=messages[-1]['input_payload'].get('completion_trigger') if messages else None)

    async def application_context(self,claim,aid):
        from interview_agent.applications.repository import ApplicationRepository
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None:raise ValueError('career_claim_expired')
            return await ApplicationRepository(self.pool).coordinator_context_at(conn,rows[0],aid)

    async def _decision_at(self,conn,goal,value):
        from interview_agent.applications.repository import ApplicationRepository
        decision=validate_decision(await self.context_at(conn,goal),value)
        if decision.application:
            c=decision.application
            view=await ApplicationRepository(self.pool).coordinator_context_at(conn,goal,c.application_id)
            action=next((a for a in view['actions'] if a['id']==c.action_id),None)
            if view['context_sha256']!=c.context_sha256 or action is None:return None
            plan=ApplicationPlan(**c.model_dump(),application_version=view['application_version'],
                package_revision=view['package_revision'],package_sha256=view['package_sha256'],
                **{k:action[k] for k in ('kind','reason','evidence_refs')})
            if decision.application_plan is not None and decision.application_plan!=plan:return None
            decision.application_plan=plan
            decision.text=plan.reason
        return decision

    async def _evidence_changed(self,conn,goal,step):
        await conn.execute("UPDATE app.career_steps SET status='FAILED',lease_until=NULL,error_code='application_evidence_changed' WHERE id=%s",(step['id'],))
        await conn.execute("UPDATE app.career_goals SET status='FAILED',revision=revision+1 WHERE id=%s",(goal['id'],))

    async def context(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None: raise ValueError('career_claim_expired')
            return await self.context_at(conn,rows[0])

    async def cached(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None: raise ValueError('career_claim_expired')
            return rows[1]['candidate']

    async def save(self,claim,candidate,origin,trace=None):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None: return False
            decision=await self._decision_at(conn,rows[0],candidate)
            if decision is None:
                await self._evidence_changed(conn,*rows);return False
            await conn.execute('UPDATE app.career_steps SET candidate=%s,origin=%s,tool_log=%s WHERE id=%s',
                (Jsonb(decision.model_dump(mode='json')),origin,Jsonb(trace or []),claim.message.operation_id))
            return True

    async def complete(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None: return False
            goal,step=rows
            if step['candidate'] is None: raise ValueError('decision_required')
            decision=await self._decision_at(conn,goal,step['candidate'])
            if decision is None:
                await self._evidence_changed(conn,goal,step);return False
            status={'ask_user':'WAITING_FOR_USER','prepare_application_action':'WAITING_FOR_USER','finish':'COMPLETED'}.get(decision.type,'WAITING_FOR_TOOL')
            await conn.execute("UPDATE app.career_steps SET status='SUCCEEDED',lease_until=NULL WHERE id=%s",(step['id'],))
            await conn.execute('UPDATE app.career_goals SET status=%s,profile=%s,action=%s,revision=revision+1 WHERE id=%s',
                (status,Jsonb(decision.fields.model_dump(mode='json')),Jsonb(decision.model_dump(mode='json')),goal['id']))
            if decision.type=='prepare_job_research' and (await self.context_at(conn,goal)).conditions.auto_research:
                from .research_repository import ResearchRepository
                await ResearchRepository(self.pool).delegate(conn,goal,step,decision)
            if decision.type=='prepare_training':
                await self._auto_prepare(conn,goal,step)
            return True

    async def _auto_prepare(self, conn, goal, step):
        """F07Z-166: one explicit training turn prepares the task; nobody has to click again.

        Only the current turn's own request counts (`request='training'`). A saved JD, a page
        load, a completion wake-up or a historical turn can never start a preparation. The task
        is created in this same transaction, under the goal lock, so the proposal and the task
        commit together; the preparation worker picks the row up from its own outbox.
        """
        payload = step['input_payload'] or {}
        if payload.get('request') != 'training':
            return
        from .roles import resolve_role
        direction = (goal['conditions'] or {}).get('direction') or ''
        selected = await (await conn.execute(
            '''SELECT s.payload->>'title' AS title FROM app.career_job_selections sel
               JOIN app.career_job_snapshots s ON s.id=sel.snapshot_id
               WHERE sel.goal_id=%s AND sel.goal_version=%s''', (goal['id'], goal['version']))).fetchone()
        role = resolve_role(direction=direction, title=(selected or {}).get('title') or '',
                            message=step['message'] or '')
        if role is None:
            # The only missing fact is the role; ask once in the original input instead of guessing.
            from psycopg.types.json import Jsonb
            text = ('这场按 Java 后端还是 AI 应用 / Agent 方向准备？可以在上面的「确认方向」里写明，'
                    '再点一次开始准备。')
            await conn.execute(
                "UPDATE app.career_goals SET status='WAITING_FOR_USER',action=%s,revision=revision+1 WHERE id=%s",
                (Jsonb({'type': 'ask_user', 'text': text, 'fields': {}}), goal['id']))
            return
        from ..interview_prep.contracts import CreatePreparation
        from ..interview_prep.repository import PreparationRepository
        keep_resume = payload.get('use_resume')
        value = CreatePreparation(expected_version=goal['version'], role=role, mode='interview',
                                  use_resume=True if keep_resume is None else bool(keep_resume))
        try:
            await PreparationRepository(self.pool).create_at(
                conn, str(goal['user_id']), goal['id'], f"training-{step['id']}", value)
        except ApiError as error:
            # A concurrent/previous turn already owns an active preparation for this goal; the
            # workbench reads that one by goal, so losing this race must not fail the turn.
            if error.code not in ('preparation_in_progress', 'idempotency_conflict'):
                raise

    async def fail_assessment(self,claim,*,waiting=False,code='coordinator_failed'):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None: return False
            status='WAITING_CREDENTIALS' if waiting else 'FAILED'
            await conn.execute('UPDATE app.career_steps SET status=%s,lease_until=NULL,error_code=%s WHERE id=%s',
                (status,code,claim.message.operation_id))
            await conn.execute('UPDATE app.career_goals SET status=%s,revision=revision+1 WHERE id=%s',(status,claim.resource_id))
            return True

    async def fail_attempt(self,claim):
        return await self.fail_assessment(claim)

    async def can_ack(self,message):
        async with self.pool.connection() as conn:
            row=await (await conn.execute('SELECT s.*,g.version AS current_version FROM app.career_steps s JOIN app.career_goals g ON g.id=s.goal_id WHERE s.id=%s AND s.job_id=%s',
                (message.operation_id,message.job_id))).fetchone()
            return row is None or row['goal_version']!=message.resource_version or row['goal_version']!=row['current_version'] or row['status'] not in ('PENDING','RUNNING')

    async def admit_scripted(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if rows is None: raise ValueError('career_claim_expired')
            goal,step=rows
            if goal['coordination_research_requests']>=12 or legacy_budget_exhausted(goal) or step['model_requests']>=3: raise ValueError('career_request_limit')
            await conn.execute('UPDATE app.career_steps SET model_requests=model_requests+1 WHERE id=%s',(step['id'],))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,coordination_research_requests=coordination_research_requests+1,revision=revision+1 WHERE id=%s',(goal['id'],))
