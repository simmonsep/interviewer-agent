"""Durable materials commands and fenced execution; facts require explicit owner confirmation."""
from interview_agent.career.budget import NON_CODING_REQUEST_ALLOCATION, legacy_budget_exhausted
import hashlib
from uuid import UUID
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim,duration
from interview_agent.career.repository import CareerRepository,digest
from .contracts import FactProposal,grounded,validate_result,render_draft
from .provenance import item_provenance
from .segments import MAX_REQUESTS

TERMINAL={'SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS'}


class MaterialRepository:
    def __init__(self,pool):self.pool=pool

    async def _state(self,conn,goal_id):
        await conn.execute('INSERT INTO app.career_material_state(goal_id) VALUES(%s) ON CONFLICT DO NOTHING',(goal_id,))
        return await (await conn.execute('SELECT * FROM app.career_material_state WHERE goal_id=%s FOR UPDATE',(goal_id,))).fetchone()

    async def _user(self,conn,principal,identity,value=None):
        await require_session(conn,principal)
        goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
        state=await self._state(conn,identity)
        if value is not None:
            if goal['version']!=value.expected_version:raise ApiError('version_conflict',409)
            if state['revision']!=value.expected_revision:raise ApiError('material_revision_conflict',409)
            if goal['status']=='CANCELLED':raise ApiError('goal_cancelled',409)
        return goal,state

    async def _prior(self,conn,table,identity,key,value,confirmation_step=None):
        # Table names are fixed internal call sites, never supplied by a client/model.
        row=await (await conn.execute(f'SELECT id,request_hash FROM app.{table} WHERE goal_id=%s AND request_id=%s',(identity,key))).fetchone()
        body=value.model_dump(mode='json')
        if body.get('draft_style')=='source':body.pop('draft_style')
        if body.get('continue_on_completion') is False:body.pop('continue_on_completion')
        if body.get('continue_after_confirmation') is False:body.pop('continue_after_confirmation')
        if confirmation_step:body['coordinator_step_id']=confirmation_step
        if row and row['request_hash']!=digest(body):raise ApiError('idempotency_conflict',409)
        return row

    async def _bump(self,conn,identity,state):
        if state['revision']>=30:raise ApiError('material_revision_limit',409)
        await conn.execute('UPDATE app.career_material_state SET revision=revision+1 WHERE goal_id=%s',(identity,))
        await conn.execute("""UPDATE app.career_material_runs SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1,
            revision=revision+1,error_code='material_changed' WHERE goal_id=%s AND status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')""",(identity,))
        return state['revision']+1

    async def upload(self,principal,identity,key,value):
        async with self.pool.connection() as conn,conn.transaction():
            await self._user(conn,principal,identity)
            prior=await self._prior(conn,'career_material_sources',identity,key,value)
            if prior:return str(prior['id']),False
            _,state=await self._user(conn,principal,identity,value)
            revision=await self._bump(conn,identity,state)
            row=await (await conn.execute('''INSERT INTO app.career_material_sources(goal_id,material_revision,request_id,request_hash,name,format,body,content_sha256)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(identity,revision,key,digest(value.model_dump(mode='json')),value.name,value.format,value.text,hashlib.sha256(value.text.encode()).hexdigest()))).fetchone()
            return str(row['id']),True

    async def _target(self,conn,goal):
        row=await (await conn.execute('''SELECT s.id,s.payload,s.content_sha256,s.source_url FROM app.career_job_selections j
            JOIN app.career_job_snapshots s ON s.id=j.snapshot_id WHERE j.goal_id=%s AND j.goal_version=%s''',(goal['id'],goal['version']))).fetchone()
        if row:
            payload=row['payload'];text=payload.get('requirements') or payload.get('visible_text') or ''
            return {'kind':'selected_job','snapshot_id':str(row['id']),'content_sha256':row['content_sha256'],
                    'title':payload.get('title',''),'requirements':text[:2500],'truncated':len(text)>2500,'source_url':row['source_url']}
        if goal['jd_text'].strip():return {'kind':'user_jd','text':goal['jd_text'],'content_sha256':hashlib.sha256(goal['jd_text'].encode()).hexdigest()}
        return {'kind':'direction','direction':goal['conditions'].get('direction',''),'qualification':'not_a_job_description'}

    async def enqueue(self,principal,identity,key,value,*,confirmation_step=None):
        if confirmation_step:
            from uuid import UUID
            confirmation_step=str(UUID(confirmation_step))
        async with self.pool.connection() as conn,conn.transaction():
            await self._user(conn,principal,identity)
            prior=await self._prior(conn,'career_material_runs',identity,key,value,confirmation_step)
            if prior:return str(prior['id']),False
            goal,state=await self._user(conn,principal,identity,value)
            handoff=None
            if confirmation_step:
                from interview_agent.career.handoffs import confirm_step
                handoff=await confirm_step(conn,goal,confirmation_step,'prepare_materials','career_material_runs')
                handoff['continue_on_completion']=getattr(value,'continue_on_completion',False)
            active=await (await conn.execute("SELECT id FROM app.career_material_runs WHERE goal_id=%s AND goal_version=%s AND material_revision=%s AND status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')",(identity,goal['version'],state['revision']))).fetchone()
            if active:raise ApiError('material_run_in_progress',409)
            count=await (await conn.execute('SELECT count(*) AS n FROM app.career_material_runs WHERE goal_id=%s',(identity,))).fetchone()
            if count['n']>=30:raise ApiError('material_run_limit',409)
            if value.source_ids:
                sources=await (await conn.execute('SELECT id,name,body AS text,content_sha256 FROM app.career_material_sources WHERE goal_id=%s AND id=ANY(%s::uuid[]) ORDER BY material_revision,id',(identity,value.source_ids))).fetchall()
                if len(sources)!=len(value.source_ids):raise ApiError('resource_unavailable',404)
            else:
                sources=await (await conn.execute('SELECT id,name,body AS text,content_sha256 FROM app.career_material_sources WHERE goal_id=%s ORDER BY material_revision DESC LIMIT 1',(identity,))).fetchall()
            # F07Z-117: detailed experience is accepted; 20,000 characters is only a DoS ceiling.
            if sum(len(s['text']) for s in sources)>20000:raise ApiError('material_scope_too_large',400)
            source_ids=[str(s['id']) for s in sources]
            facts=await (await conn.execute('''SELECT id,source_id,kind,quote,start_char,end_char FROM app.career_material_facts
                WHERE goal_id=%s AND source_id=ANY(%s::uuid[]) AND (%s OR id=ANY(%s::uuid[])) ORDER BY created_at,id LIMIT 13''',
                (identity,source_ids,not value.fact_ids,value.fact_ids))).fetchall()
            if value.fact_ids and len(facts)!=len(value.fact_ids):raise ApiError('resource_unavailable',404)
            if value.draft_style=='rewrite':facts=[]
            if len(facts)>12:raise ApiError('fact_scope_too_large_select_ids',400)
            scope={'mode':value.mode,'goal_version':goal['version'],'material_revision':state['revision'],
                   'target':await self._target(conn,goal),
                   'sources':[{**s,'id':str(s['id'])} for s in sources],
                   'facts':[{**f,'id':str(f['id']),'source_id':str(f['source_id'])} for f in facts]}
            if value.draft_style=='rewrite':
                if value.mode!='draft':raise ApiError('rewrite_requires_draft',400)
                scope['draft_style']='rewrite'
            body=value.model_dump(mode='json')
            if body.get('draft_style')=='source':body.pop('draft_style')
            if body.get('continue_on_completion') is False:body.pop('continue_on_completion')
            if handoff:
                scope['coordinator_handoff']=handoff;body['coordinator_step_id']=confirmation_step
            row=await (await conn.execute('''INSERT INTO app.career_material_runs(goal_id,goal_version,material_revision,request_id,request_hash,input_payload)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',(identity,goal['version'],state['revision'],key,digest(body),Jsonb(scope)))).fetchone()
            return str(row['id']),True

    async def confirm(self,principal,identity,key,value):
        async with self.pool.connection() as conn,conn.transaction():
            await self._user(conn,principal,identity)
            prior=await self._prior(conn,'career_material_confirmations',identity,key,value)
            if prior:return str(prior['id']),False
            goal,state=await self._user(conn,principal,identity,value)
            run=await (await conn.execute('SELECT * FROM app.career_material_runs WHERE id=%s AND goal_id=%s',(value.run_id,identity))).fetchone()
            if run is None:raise ApiError('resource_unavailable',404)
            if run['goal_version']!=goal['version'] or run['material_revision']!=state['revision'] or run['status']!='SUCCEEDED':raise ApiError('material_analysis_stale',409)
            result=validate_result(run['input_payload'],run['result'])
            if result.action!='analysis' or any(i>=len(result.proposals) for i in value.proposal_ids):raise ApiError('invalid_fact_confirmation',400)
            if value.continue_after_confirmation and goal['status'] in ('PENDING','RUNNING','WAITING_CREDENTIALS'):
                raise ApiError('coordination_in_progress',409)
            revision=await self._bump(conn,identity,state)
            batch=await (await conn.execute('''INSERT INTO app.career_material_confirmations(goal_id,run_id,material_revision,request_id,request_hash)
                VALUES(%s,%s,%s,%s,%s) RETURNING id''',(identity,run['id'],revision,key,digest({k:v for k,v in value.model_dump(mode='json').items() if k!='continue_after_confirmation' or v})))).fetchone()
            for index in value.proposal_ids:
                f=result.proposals[index]
                await conn.execute('''INSERT INTO app.career_material_facts(goal_id,confirmation_id,source_id,kind,quote,start_char,end_char)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(goal_id,source_id,start_char,end_char,kind) DO NOTHING''',
                    (identity,batch['id'],f['source_id'],f['kind'],f['quote'],f['start_char'],f['end_char']))
            if value.continue_after_confirmation:
                from interview_agent.career.handoffs import continue_user_event
                await continue_user_event(conn,goal,'facts_confirmed',batch['id'])
            return str(batch['id']),True

    async def listing(self,principal,identity):
        async with self.pool.connection() as conn,conn.transaction():
            goal,state=await self._user(conn,principal,identity)
            target=await self._target(conn,goal)
            sources=await (await conn.execute('''SELECT s.id,s.name,s.format,s.body AS text,s.material_revision,s.content_sha256,
                s.clarification_context,CASE WHEN p.id IS NULL THEN NULL ELSE jsonb_build_object('preview_id',p.id,'file_sha256',p.file_sha256,
                'byte_length',p.byte_length,'metadata',p.metadata) END AS file_reference
                FROM app.career_material_sources s LEFT JOIN app.career_material_file_previews p ON p.id=s.file_preview_id
                WHERE s.goal_id=%s ORDER BY s.material_revision DESC''',(identity,))).fetchall()
            facts=await (await conn.execute('SELECT id,source_id,kind,quote,start_char,end_char FROM app.career_material_facts WHERE goal_id=%s ORDER BY created_at,id',(identity,))).fetchall()
            runs=await (await conn.execute('SELECT * FROM app.career_material_runs WHERE goal_id=%s ORDER BY created_at DESC,id DESC LIMIT 30',(identity,))).fetchall()
            drafts=await (await conn.execute('SELECT * FROM app.career_material_drafts WHERE goal_id=%s ORDER BY version DESC LIMIT 30',(identity,))).fetchall()
            def stale(row,target_value):return row['goal_version']!=goal['version'] or row['material_revision']!=state['revision'] or digest(target_value)!=digest(target) or goal['status']=='CANCELLED'
            return {'goal_version':goal['version'],'material_revision':state['revision'],'target':target,
                    'model_requests':goal['model_requests'],'material_model_requests':goal['material_model_requests'],
                    'sources':[{**s,'id':str(s['id'])} for s in sources],
                    'facts':[{**f,'id':str(f['id']),'source_id':str(f['source_id']),'verification':'user_confirmed',
                              'file_locations':[loc for s in sources if s['id']==f['source_id'] and s['file_reference']
                                for loc in s['file_reference']['metadata']['locations']
                                if loc['start_char']<f['end_char'] and loc['end_char']>f['start_char']]} for f in facts],
                    'runs':[{**{k:r[k] for k in ('status','goal_version','material_revision','revision','model_requests','result','error_code','origin')},
                             'id':str(r['id']),'mode':r['input_payload']['mode'],'stale':stale(r,r['input_payload']['target'])} for r in runs],
                    'drafts':[{**{k:d[k] for k in ('version','goal_version','material_revision','markdown','payload','created_at')},'id':str(d['id']),'run_id':str(d['run_id']) if d['run_id'] else None,'origin':d['origin'],
                               'stale':stale(d,d['payload']['target']),
                               # S00Gd: per-bullet source/version/segment/confirmation chain, read
                               # from the stored payload so the answer never drifts from the draft.
                               'provenance':item_provenance(d['payload'])} for d in drafts]}

    async def restore(self,principal,identity,draft_id,key,value):
        """Copy an older version into a new immutable version (origin='restore')."""
        draft_id=str(UUID(draft_id))
        async with self.pool.connection() as conn,conn.transaction():
            await self._user(conn,principal,identity)
            request_hash=digest({'restore':draft_id,**value.model_dump(mode='json')})
            prior=await (await conn.execute('SELECT id,request_hash FROM app.career_material_drafts WHERE goal_id=%s AND request_id=%s',(identity,key))).fetchone()
            if prior:
                if prior['request_hash']!=request_hash:raise ApiError('idempotency_conflict',409)
                return str(prior['id']),False
            goal,state=await self._user(conn,principal,identity,value)
            source=await (await conn.execute('SELECT * FROM app.career_material_drafts WHERE id=%s AND goal_id=%s',(draft_id,identity))).fetchone()
            if not source:raise ApiError('resource_unavailable',404)
            latest=await (await conn.execute('SELECT id,version FROM app.career_material_drafts WHERE goal_id=%s ORDER BY version DESC LIMIT 1',(identity,))).fetchone()
            payload=dict(source['payload'])
            payload['target']=await self._target(conn,goal)
            from .authoring import build as authoring_record
            payload['authoring_record']={**authoring_record(payload,origin='restore',version=latest['version']+1,
                parent_version=latest['version']),'restored_from_version':source['version']}
            row=await (await conn.execute('''INSERT INTO app.career_material_drafts
                (goal_id,version,goal_version,material_revision,payload,markdown,parent_draft_id,request_id,request_hash,origin)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'restore') RETURNING id''',
                (identity,latest['version']+1,goal['version'],state['revision'],Jsonb(payload),source['markdown'],
                 latest['id'],key,request_hash))).fetchone()
            return str(row['id']),True

    async def cancel(self,principal,identity,run_id,value):
        async with self.pool.connection() as conn,conn.transaction():
            await self._user(conn,principal,identity,value)
            row=await (await conn.execute('SELECT status FROM app.career_material_runs WHERE id=%s AND goal_id=%s FOR UPDATE',(run_id,identity))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            if row['status'] in ('PENDING','RUNNING','WAITING_CREDENTIALS'):
                await conn.execute("UPDATE app.career_material_runs SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1,revision=revision+1,error_code='user_cancelled' WHERE id=%s",(run_id,))

    async def _lock(self,conn,message):
        ref=await (await conn.execute('SELECT goal_id FROM app.career_material_runs WHERE id=%s AND job_id=%s',(message.operation_id,message.job_id))).fetchone()
        if not ref:return None
        goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(ref['goal_id'],))).fetchone()
        state=await self._state(conn,ref['goal_id'])
        run=await (await conn.execute('SELECT * FROM app.career_material_runs WHERE id=%s FOR UPDATE',(message.operation_id,))).fetchone()
        return (goal,state,run) if goal and run else None

    async def _current(self,conn,rows):
        goal,state,run=rows
        return (goal['status']!='CANCELLED' and goal['version']==run['goal_version'] and state['revision']==run['material_revision']
                and digest(await self._target(conn,goal))==digest(run['input_payload']['target']))

    async def _owned(self,conn,claim):
        rows=await self._lock(conn,claim.message)
        if not rows:return None
        goal,state,run=rows
        now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (run['status']!='RUNNING' or run['attempt']!=claim.attempt or run['fencing_token']!=claim.fencing_token or run['lease_until']<=now
            or str(goal['user_id'])!=claim.user_id or str(goal['id'])!=claim.resource_id or run['goal_version']!=claim.message.resource_version
            or not await self._current(conn,rows)):return None
        return rows

    async def pending_dispatch(self,recovery_seconds=5):
        async with self.pool.connection() as conn,conn.transaction():
            await conn.execute("""UPDATE app.career_material_runs r SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1,
                revision=r.revision+1,error_code='input_changed' FROM app.career_goals g,app.career_material_state s
                WHERE g.id=r.goal_id AND s.goal_id=g.id AND r.status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')
                AND (g.version<>r.goal_version OR g.status='CANCELLED' OR s.revision<>r.material_revision OR EXISTS
                  (SELECT 1 FROM app.career_job_selections j WHERE j.goal_id=g.id AND j.goal_version=g.version AND j.snapshot_id::text IS DISTINCT FROM r.input_payload#>>'{target,snapshot_id}'))""")
            return await (await conn.execute("""SELECT id AS outbox_id,id AS operation_id,job_id,goal_version AS resource_version FROM app.career_material_runs
                WHERE (status='PENDING' OR (status='RUNNING' AND lease_until<=clock_timestamp()))
                AND (dispatched_at IS NULL OR dispatched_at<=clock_timestamp()-(%s*interval '1 second')) ORDER BY created_at LIMIT 100""",(recovery_seconds,))).fetchall()

    async def mark_dispatched(self,identity):
        async with self.pool.connection() as conn:await conn.execute('UPDATE app.career_material_runs SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s',(identity,))

    async def claim(self,message,lease_seconds=30):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            if not rows:return None
            goal,state,run=rows
            if run['status'] not in ('PENDING','RUNNING') or run['goal_version']!=message.resource_version:return None
            if not await self._current(conn,rows):
                await conn.execute("UPDATE app.career_material_runs SET status='CANCELLED',lease_until=NULL,revision=revision+1,error_code='input_changed' WHERE id=%s",(run['id'],));return None
            now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if run['status']=='RUNNING' and run['lease_until']>now:return None
            if run['attempt']>=3:
                await conn.execute("UPDATE app.career_material_runs SET status='FAILED',lease_until=NULL,revision=revision+1,error_code='attempt_limit' WHERE id=%s",(run['id'],))
                from interview_agent.career.handoffs import pause_tool_parent
                await pause_tool_parent(conn,goal['id'],run['input_payload'],'attempt_limit','materials')
                return None
            row=await (await conn.execute("""UPDATE app.career_material_runs SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,
                revision=revision+1,lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s RETURNING *""",(duration(lease_seconds),run['id']))).fetchone()
            return Claim(message,str(goal['user_id']),str(goal['id']),row['attempt'],row['fencing_token'],'',kind='material_agent')

    async def heartbeat(self,claim,lease_seconds=30):
        async with self.pool.connection() as conn,conn.transaction():
            if not await self._owned(conn,claim):return False
            await conn.execute("UPDATE app.career_material_runs SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",(duration(lease_seconds),claim.message.operation_id));return True

    async def context(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise ValueError('material_claim_expired')
            return rows[2]['input_payload']

    async def cached(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise ValueError('material_claim_expired')
            return rows[2]['candidate']

    async def admit_scripted(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            # F07Z-51: this counter is the fuse, not the batch plan. The agent derives the
            # tighter ceiling from the batch count (`segments.request_ceiling`); a second
            # hardcoded number here silently overrode it from F07Z-44 to F07Z-50.
            # F07Z-120: the goal fuse reuses the non-coding allocation, so the scripted path and
            # the live ledger agree on the same capacity (F07Z-167 raised the pool to 120).
            if not rows or legacy_budget_exhausted(rows[0]) or rows[0]['material_model_requests']>=NON_CODING_REQUEST_ALLOCATION or rows[2]['model_requests']>=MAX_REQUESTS:raise ValueError('material_model_limit')
            await conn.execute('UPDATE app.career_material_runs SET model_requests=model_requests+1 WHERE id=%s',(claim.message.operation_id,))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,material_model_requests=material_model_requests+1,revision=revision+1 WHERE id=%s',(claim.resource_id,))

    async def save(self,claim,value,origin):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            result=validate_result(rows[2]['input_payload'],value)
            await conn.execute('UPDATE app.career_material_runs SET candidate=%s,origin=%s WHERE id=%s',(Jsonb(result.model_dump(mode='json')),origin,claim.message.operation_id));return True

    async def complete(self,claim):
        try:
            return await self._complete(claim)
        except ValueError as error:
            # A candidate that cannot be rendered must say why (F07Z-46): the generic worker
            # path only retries and finally reports `material_failed`.
            await self.fail_assessment(claim,code='material_draft_rejected',
                                       diagnostic={'reason':str(error)[:120]})
            return False

    async def _complete(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            goal,state,run=rows;scope=run['input_payload'];result=validate_result(scope,run['candidate'])
            if result.action=='draft':
                markdown,payload=render_draft(scope,result)
                previous=await (await conn.execute('SELECT coalesce(max(version),0) AS v FROM app.career_material_drafts WHERE goal_id=%s',(goal['id'],))).fetchone()
                version=int(previous['v'])+1
                from .authoring import build as authoring_record
                payload['authoring_record']=authoring_record(payload,origin='agent_rewrite',
                    version=version,parent_version=int(previous['v']) or None)
                row=await (await conn.execute('''INSERT INTO app.career_material_drafts(goal_id,run_id,version,goal_version,material_revision,payload,markdown)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,version''',
                    (goal['id'],run['id'],version,run['goal_version'],run['material_revision'],Jsonb(payload),markdown))).fetchone()
            await conn.execute("UPDATE app.career_material_runs SET status='SUCCEEDED',lease_until=NULL,result=%s,revision=revision+1 WHERE id=%s",(Jsonb(result.model_dump(mode='json')),run['id']))
            from interview_agent.career.handoffs import continue_after
            await continue_after(conn,goal,scope,'materials',run['id'],'SUCCEEDED')
            return True

    async def fail_assessment(self,claim,**kwargs):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            await conn.execute('UPDATE app.career_material_runs SET status=%s,lease_until=NULL,error_code=%s,result=%s,revision=revision+1 WHERE id=%s',
                               ('WAITING_CREDENTIALS' if kwargs.get('waiting') else 'FAILED',kwargs.get('code','material_failed'),Jsonb(kwargs['diagnostic']) if kwargs.get('diagnostic') else None,claim.message.operation_id))
            if kwargs.get('waiting'):
                from interview_agent.career.handoffs import pause_tool_parent
                await pause_tool_parent(conn,rows[0]['id'],rows[2]['input_payload'],'admission_wait','materials')
            if not kwargs.get('waiting'):
                from interview_agent.career.handoffs import continue_after
                resumed=await continue_after(conn,rows[0],rows[2]['input_payload'],'materials',rows[2]['id'],'FAILED')
                if not resumed:
                    from interview_agent.career.handoffs import pause_tool_parent
                    await pause_tool_parent(conn,rows[0]['id'],rows[2]['input_payload'],'task_failed','materials')
            return True

    async def fail_attempt(self,claim):return await self.fail_assessment(claim)

    async def can_ack(self,message):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            return not rows or rows[2]['status'] in TERMINAL or not await self._current(conn,rows)
