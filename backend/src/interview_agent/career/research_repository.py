"""Durable source reads and immutable content versions; goal version fences commits."""
from interview_agent.career.budget import legacy_budget_exhausted
import hashlib
import json
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim, duration
from .repository import CareerRepository, digest
from .content import review_flags, visible_text
from .research_tools import ResearchInput, ResearchBundle, checked_items
from .contracts import Conditions
from .matching import checks

TERMINAL = {'SUCCEEDED','PARTIAL','FAILED','CANCELLED','WAITING_CREDENTIALS'}


class ResearchRepository:
    def __init__(self,pool): self.pool=pool

    async def delegate(self,conn,goal,step,decision):
        conditions=Conditions.model_validate(goal['conditions'])
        hint=decision.fields.direction.model_dump() if decision.fields.direction else None
        value=ResearchInput(expected_version=goal['version'],kind='search',keyword=conditions.direction or hint['value'])
        payload={'request':value.model_dump(mode='json'),'conditions':conditions.model_dump(),'jd_text':'','direction_hint':hint}
        payload['coordinator_handoff']={'step_id':str(step['id']),'goal_version':goal['version'],
            'continue_on_completion':bool(step['input_payload'].get('continue_after_research')),'action':'prepare_job_research'}
        await conn.execute('''INSERT INTO app.career_research_runs(goal_id,goal_version,request_id,request_hash,input_payload,mode,parent_step_id)
            VALUES(%s,%s,%s,%s,%s,'agent',%s) ON CONFLICT(parent_step_id) DO NOTHING''',
            (goal['id'],goal['version'],'parent-'+str(step['id']),digest(payload),Jsonb(payload),step['id']))

    async def enqueue(self,principal,identity,key,value):
        hashed=digest(value.model_dump(mode='json'))
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
            prior=await (await conn.execute('SELECT id,request_hash FROM app.career_research_runs WHERE goal_id=%s AND request_id=%s',(identity,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed: raise ApiError('idempotency_conflict',409)
                return str(prior['id']),False
            if goal['version']!=value.expected_version: raise ApiError('version_conflict',409)
            if goal['status']=='CANCELLED': raise ApiError('goal_cancelled',409)
            if Conditions.model_validate(goal['conditions']).auto_research and goal['status'] in ('PENDING','RUNNING'):
                raise ApiError('coordination_in_progress',409)
            if value.kind=='import_jd' and not goal['jd_text'].strip(): raise ApiError('saved_jd_required',400)
            active=await (await conn.execute("SELECT id FROM app.career_research_runs WHERE goal_id=%s AND goal_version=%s AND status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')",(identity,goal['version']))).fetchone()
            if active: raise ApiError('research_in_progress',409)
            payload={'request':value.model_dump(mode='json'),'conditions':goal['conditions'],'jd_text':goal['jd_text']}
            row=await (await conn.execute('''INSERT INTO app.career_research_runs(goal_id,goal_version,request_id,request_hash,input_payload)
                VALUES(%s,%s,%s,%s,%s) RETURNING id''',(identity,goal['version'],key,hashed,Jsonb(payload)))).fetchone()
            return str(row['id']),True

    async def _position_for(self,conn,goal_id,source_id,source_job_id):
        """Stable position id for this source triple; created once, never keyed on JD text.

        A self-authored job currently uses the content digest as `source_job_id`; phase 2.2 will
        let the user edit that JD under the same position, so the binding key must live here
        rather than in the snapshot.
        """
        row=await (await conn.execute('''INSERT INTO app.career_positions(goal_id,source_id,source_job_id)
            VALUES(%s,%s,%s) ON CONFLICT(goal_id,source_id,source_job_id) DO NOTHING RETURNING id''',
            (goal_id,source_id,source_job_id))).fetchone()
        if row:return row['id']
        row=await (await conn.execute('''SELECT id FROM app.career_positions
            WHERE goal_id=%s AND source_id=%s AND source_job_id=%s''',(goal_id,source_id,source_job_id))).fetchone()
        return row['id']

    async def _point_at_snapshot(self,conn,position_id,snapshot_id,*,bump=False):
        await conn.execute('''UPDATE app.career_positions
            SET current_snapshot_id=%s, version=version+%s, updated_at=clock_timestamp()
            WHERE id=%s''',(snapshot_id,1 if bump else 0,position_id))

    async def create_manual(self,principal,identity,key,value):
        """Store a job the user typed in, without touching the network.

        A manual job is still a normal snapshot plus one observation, so selection,
        the city/recruitment checks and every downstream panel behave exactly as
        they do for a fetched job. What differs is provenance: source_id is
        `user-jd`, and the run is completed on the spot because there is nothing
        to fetch and nothing to delegate to a worker.
        """
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
            if goal['version']!=value.expected_version or goal['status']=='CANCELLED':
                raise ApiError('version_conflict',409)
            request={'kind':'manual','company':value.company,'title':value.title,
                     'cities':value.cities,'url':value.url,'recruitment_label':value.recruitment_label}
            payload={'request':request,'conditions':goal['conditions'],'jd_text':''}
            hashed=digest(payload)
            prior=await (await conn.execute('SELECT id,request_hash FROM app.career_research_runs WHERE goal_id=%s AND request_id=%s',(identity,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed: raise ApiError('idempotency_conflict',409)
                row=await (await conn.execute('SELECT snapshot_id FROM app.career_job_observations WHERE run_id=%s LIMIT 1',(prior['id'],))).fetchone()
                return (str(row['snapshot_id']) if row else None),False
            cleaned=visible_text(value.jd_text)
            if not cleaned: raise ApiError('empty_visible_jd',400)
            # The identity covers company and title too: the same JD text pasted for two
            # different roles is two different jobs, and the same job saved twice must
            # collide on the unique key instead of creating a duplicate row.
            digest_hex=hashlib.sha256('\x00'.join([value.company,value.title,'、'.join(value.cities),value.jd_text]).encode()).hexdigest()
            job={'title':value.title,'company':value.company,'cities':value.cities,
                 'recruitment_label':value.recruitment_label or None,
                 'responsibilities':'','requirements':'',
                 'raw_text':value.jd_text,'visible_text':cleaned,'availability':'unknown',
                 'content_warnings':review_flags(cleaned)}
            verdict=checks(job,goal['conditions'],goal['version'])
            city=next((row for row in verdict['checks'] if row['field']=='cities'),None)
            city_check={'pass':'match','conflict':'conflict','unknown':'unknown'}.get(
                city['status'] if city else 'unknown','unknown')
            # Saving the identical job again changes nothing: reuse the snapshot instead
            # of writing another run and another observation that say the same thing.
            existing=await (await conn.execute("SELECT id FROM app.career_job_snapshots WHERE goal_id=%s AND source_id='user-jd' AND source_job_id=%s AND content_sha256=%s",
                (identity,digest_hex,digest_hex))).fetchone()
            if existing: return str(existing['id']),False
            included=city_check!='conflict'
            position_id=await self._position_for(conn,identity,'user-jd',digest_hex)
            run=await (await conn.execute("INSERT INTO app.career_research_runs(goal_id,goal_version,request_id,request_hash,input_payload,status,mode) VALUES(%s,%s,%s,%s,%s,'SUCCEEDED','manual') RETURNING id",
                (identity,goal['version'],key,hashed,Jsonb(payload)))).fetchone()
            # Saving the same job twice is a no-op, not an error: the unique key on
            # (goal, source, source_job_id, content hash) decides.
            snapshot=await (await conn.execute("INSERT INTO app.career_job_snapshots(goal_id,source_id,source_job_id,content_sha256,source_url,retrieval_url,payload,position_id) VALUES(%s,'user-jd',%s,%s,%s,NULL,%s,%s) ON CONFLICT(goal_id,source_id,source_job_id,content_sha256) DO NOTHING RETURNING id",
                (identity,digest_hex,digest_hex,value.url or None,Jsonb(job),position_id))).fetchone()
            if not snapshot:
                snapshot=await (await conn.execute("SELECT id FROM app.career_job_snapshots WHERE goal_id=%s AND source_id='user-jd' AND source_job_id=%s AND content_sha256=%s",
                    (identity,digest_hex,digest_hex))).fetchone()
            await self._point_at_snapshot(conn,position_id,snapshot['id'])
            from .bindings import BindingRepository
            await BindingRepository(self.pool).auto_bind(conn,identity,position_id)
            await conn.execute("INSERT INTO app.career_job_observations(run_id,snapshot_id,observed_at,response_sha256,city_check,included) VALUES(%s,%s,clock_timestamp(),%s,%s,%s)",
                (run['id'],snapshot['id'],digest_hex,city_check,included))
            # The matches list needs the snapshot id, so the result is written after the
            # insert. Only `result` is updated: input_payload stays immutable.
            result={'origin':'user_provided','search':None,'readable':1,'included':1 if included else 0,
                    'failures':[],'coverage':'manual','availability':'unknown',
                    'matches':[{'snapshot_id':str(snapshot['id']),**verdict,'agent_reviewed':False}],
                    'agent':None,'executed_request':request}
            await conn.execute('UPDATE app.career_research_runs SET result=%s WHERE id=%s',(Jsonb(result),run['id']))
            return str(snapshot['id']),True

    async def _manual_job(self,goal,value):
        """The normalized job payload and the checks shared by creation and revision."""
        request={'kind':'manual','company':value.company,'title':value.title,
                 'cities':value.cities,'url':value.url,'recruitment_label':value.recruitment_label}
        cleaned=visible_text(value.jd_text)
        if not cleaned: raise ApiError('empty_visible_jd',400)
        digest_hex=hashlib.sha256('\x00'.join([value.company,value.title,'、'.join(value.cities),value.jd_text]).encode()).hexdigest()
        job={'title':value.title,'company':value.company,'cities':value.cities,
             'recruitment_label':value.recruitment_label or None,
             'responsibilities':'','requirements':'',
             'raw_text':value.jd_text,'visible_text':cleaned,'availability':'unknown',
             'content_warnings':review_flags(cleaned)}
        verdict=checks(job,goal['conditions'],goal['version'])
        city=next((row for row in verdict['checks'] if row['field']=='cities'),None)
        city_check={'pass':'match','conflict':'conflict','unknown':'unknown'}.get(
            city['status'] if city else 'unknown','unknown')
        return request,digest_hex,job,verdict,city_check

    async def revise_manual(self,principal,identity,position_id,key,value):
        """Edit the JD of an existing self-added position; the position id does not change."""
        position_id=str(position_id)
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
            if goal['status']=='CANCELLED':raise ApiError('goal_cancelled',409)
            position=await (await conn.execute('''SELECT * FROM app.career_positions
                WHERE id=%s AND goal_id=%s FOR UPDATE''',(position_id,identity))).fetchone()
            if not position:raise ApiError('resource_unavailable',404)
            if position['source_id']!='user-jd':raise ApiError('manual_position_required',400)
            request,digest_hex,job,verdict,city_check=await self._manual_job(goal,value)
            request_payload={'position_id':position_id,'expected_version':value.expected_version,'request':request}
            hashed=digest(request_payload)
            prior=await (await conn.execute('SELECT id,request_hash FROM app.career_research_runs WHERE goal_id=%s AND request_id=%s',(identity,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                row=await (await conn.execute('SELECT snapshot_id FROM app.career_job_observations WHERE run_id=%s LIMIT 1',(prior['id'],))).fetchone()
                return (str(row['snapshot_id']) if row else None),False
            if position['version']!=value.expected_version:raise ApiError('position_version_conflict',409)
            current=await (await conn.execute('SELECT id,content_sha256 FROM app.career_job_snapshots WHERE id=%s',(position['current_snapshot_id'],))).fetchone() if position['current_snapshot_id'] else None
            if current and current['content_sha256']==digest_hex:
                # Identical content is a no-op, not a new version.
                return str(current['id']),False
            included=city_check!='conflict'
            run=await (await conn.execute("INSERT INTO app.career_research_runs(goal_id,goal_version,request_id,request_hash,input_payload,status,mode) VALUES(%s,%s,%s,%s,%s,'SUCCEEDED','manual') RETURNING id",
                (identity,goal['version'],key,hashed,Jsonb({'request':request,'conditions':goal['conditions'],'jd_text':''})))).fetchone()
            snapshot=await (await conn.execute('''INSERT INTO app.career_job_snapshots
                (goal_id,source_id,source_job_id,content_sha256,source_url,retrieval_url,payload,position_id)
                VALUES(%s,'user-jd',%s,%s,%s,NULL,%s,%s) ON CONFLICT(goal_id,source_id,source_job_id,content_sha256) DO NOTHING RETURNING id''',
                (identity,position['source_job_id'],digest_hex,value.url or None,Jsonb(job),position_id))).fetchone()
            if not snapshot:
                snapshot=await (await conn.execute('''SELECT id FROM app.career_job_snapshots
                    WHERE goal_id=%s AND source_id='user-jd' AND source_job_id=%s AND content_sha256=%s''',
                    (identity,position['source_job_id'],digest_hex))).fetchone()
            await self._point_at_snapshot(conn,position_id,snapshot['id'],bump=True)
            await conn.execute("INSERT INTO app.career_job_observations(run_id,snapshot_id,observed_at,response_sha256,city_check,included) VALUES(%s,%s,clock_timestamp(),%s,%s,%s)",
                (run['id'],snapshot['id'],digest_hex,city_check,included))
            result={'origin':'user_provided','search':None,'readable':1,'included':1 if included else 0,
                    'failures':[],'coverage':'manual','availability':'unknown',
                    'matches':[{'snapshot_id':str(snapshot['id']),**verdict,'agent_reviewed':False}],
                    'agent':None,'executed_request':request}
            await conn.execute('UPDATE app.career_research_runs SET result=%s WHERE id=%s',(Jsonb(result),run['id']))
            return str(snapshot['id']),True

    async def listing(self,principal,identity):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
            rows=await (await conn.execute('''SELECT id,goal_version,status,revision,input_payload,result,error_code,created_at,mode,agent_origin,model_requests,source_requests,parent_step_id
                FROM app.career_research_runs WHERE goal_id=%s ORDER BY created_at DESC,id DESC LIMIT 20''',(identity,))).fetchall()
            items=await (await conn.execute('''SELECT o.*,s.source_id,s.source_job_id,s.content_sha256,s.source_url,s.retrieval_url,s.payload,
                    s.position_id,p.version AS position_version,p.current_snapshot_id
                FROM app.career_job_observations o JOIN app.career_job_snapshots s ON s.id=o.snapshot_id
                JOIN app.career_research_runs r ON r.id=o.run_id
                LEFT JOIN app.career_positions p ON p.id=s.position_id
                WHERE r.goal_id=%s AND r.goal_version=%s ORDER BY o.observed_at DESC,s.id LIMIT 100''',(identity,goal['version']))).fetchall()
            selected=await (await conn.execute('SELECT id,run_id,snapshot_id,acknowledged_unknown FROM app.career_job_selections WHERE goal_id=%s AND goal_version=%s',(identity,goal['version']))).fetchone()
            return {'goal_version':goal['version'],'goal_status':goal['status'],
                    'selected':{**dict(selected),'id':str(selected['id']),'run_id':str(selected['run_id']),'snapshot_id':str(selected['snapshot_id'])} if selected else None,
                    'runs':[{**{k:r[k] for k in ('goal_version','status','revision','result','error_code','created_at','mode','agent_origin','model_requests','source_requests')},
                             'parent_step_id':str(r['parent_step_id']) if r['parent_step_id'] else None,
                             'id':str(r['id']),'request':r['input_payload']['request'],'stale':r['goal_version']!=goal['version']} for r in rows],
                    'items':[{**dict(x),'snapshot_id':str(x['snapshot_id']),'run_id':str(x['run_id'])} for x in items]}

    async def pending_dispatch(self,recovery_seconds=5):
        async with self.pool.connection() as conn,conn.transaction():
            # Mark superseded durable work terminal even if no Redis message ever existed.
            await conn.execute("""UPDATE app.career_research_runs r SET status='CANCELLED',lease_until=NULL,
                fencing_token=fencing_token+1,revision=r.revision+1,error_code='goal_changed'
                FROM app.career_goals g WHERE g.id=r.goal_id AND (g.version<>r.goal_version OR g.status='CANCELLED')
                AND r.status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')""")
            return await (await conn.execute('''SELECT id AS outbox_id,id AS operation_id,job_id,goal_version AS resource_version
                FROM app.career_research_runs WHERE (status='PENDING' OR (status='RUNNING' AND lease_until<=clock_timestamp()))
                AND (dispatched_at IS NULL OR dispatched_at<=clock_timestamp()-(%s*interval '1 second'))
                ORDER BY created_at LIMIT 100''',(recovery_seconds,))).fetchall()

    async def mark_dispatched(self,identity):
        async with self.pool.connection() as conn:
            await conn.execute('UPDATE app.career_research_runs SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s',(identity,))

    async def _lock(self,conn,message):
        ref=await (await conn.execute('SELECT goal_id FROM app.career_research_runs WHERE id=%s AND job_id=%s',(message.operation_id,message.job_id))).fetchone()
        if not ref: return None
        goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(ref['goal_id'],))).fetchone()
        run=await (await conn.execute('SELECT * FROM app.career_research_runs WHERE id=%s FOR UPDATE',(message.operation_id,))).fetchone()
        return (goal,run) if goal and run else None

    async def _owned(self,conn,claim):
        rows=await self._lock(conn,claim.message)
        if not rows:return None
        goal,run=rows
        now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (run['status']!='RUNNING' or run['attempt']!=claim.attempt or run['fencing_token']!=claim.fencing_token
            or run['lease_until']<=now or goal['version']!=run['goal_version'] or goal['status']=='CANCELLED'
            or str(goal['user_id'])!=claim.user_id or str(goal['id'])!=claim.resource_id
            or goal['version']!=claim.message.resource_version): return None
        return rows

    async def claim(self,message,lease_seconds=30):
        seconds=duration(lease_seconds)
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            if not rows:return None
            goal,run=rows
            now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if run['status'] not in ('PENDING','RUNNING'):return None
            if run['goal_version']!=message.resource_version:return None
            if goal['version']!=run['goal_version'] or goal['status']=='CANCELLED':
                await conn.execute("UPDATE app.career_research_runs SET status='CANCELLED',lease_until=NULL,revision=revision+1,error_code='goal_changed' WHERE id=%s",(run['id'],));return None
            if run['status']=='RUNNING' and run['lease_until']>now:return None
            if run['attempt']>=3:
                await conn.execute("UPDATE app.career_research_runs SET status='FAILED',lease_until=NULL,revision=revision+1,error_code='attempt_limit' WHERE id=%s",(run['id'],))
                from interview_agent.career.handoffs import pause_tool_parent
                await pause_tool_parent(conn,goal['id'],run['input_payload'],'attempt_limit','research')
                return None
            row=await (await conn.execute("""UPDATE app.career_research_runs SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,
                revision=revision+1,lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s RETURNING *""",(seconds,run['id']))).fetchone()
            return Claim(message,str(goal['user_id']),str(goal['id']),row['attempt'],row['fencing_token'],'',kind='research_agent' if run['mode']=='agent' else 'research_jobs')

    async def heartbeat(self,claim,lease_seconds=30):
        async with self.pool.connection() as conn,conn.transaction():
            if not await self._owned(conn,claim):return False
            await conn.execute("UPDATE app.career_research_runs SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",(duration(lease_seconds),claim.message.operation_id))
            return True

    async def context(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise ValueError('research_claim_expired')
            return rows[1]['input_payload']

    @staticmethod
    def scope(payload,version):
        conditions=Conditions.model_validate(payload['conditions']).model_dump()
        return {'goal_version':version,'conditions':{k:conditions[k] for k in ('direction','cities','city_policy','recruitment')},
                'direction_hint':(payload.get('direction_hint') or {}).get('value'),'allowed_source':'tme-campus'}

    async def agent_scope(self,claim):
        return self.scope(await self.context(claim),claim.message.resource_version)

    async def admit_scripted(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows or rows[0]['coordination_research_requests']>=12 or legacy_budget_exhausted(rows[0]) or rows[1]['model_requests']>=6:raise ValueError('research_model_limit')
            await conn.execute('UPDATE app.career_research_runs SET model_requests=model_requests+1 WHERE id=%s',(claim.message.operation_id,))
            await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,coordination_research_requests=coordination_research_requests+1,revision=revision+1 WHERE id=%s',(claim.resource_id,))

    async def admit_source(self,claim):
        from interview_agent.billing.ledger import BillingUnavailable
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise BillingUnavailable('model_execution_expired')
            goal,run=rows
            if run['mode']!='agent' or goal['source_requests']>=12 or run['source_requests']>=12:raise BillingUnavailable('source_budget_exhausted')
            await conn.execute('UPDATE app.career_research_runs SET source_requests=source_requests+1 WHERE id=%s',(run['id'],))
            await conn.execute('UPDATE app.career_goals SET source_requests=source_requests+1,revision=revision+1 WHERE id=%s',(goal['id'],))

    def items(self,run,bundle):
        payload=run['input_payload'];value=ResearchInput.model_validate(payload['request'])
        if run['mode']=='agent':
            if bundle.agent is None:raise ValueError('research_report_required')
            if bundle.agent.action=='ask_user' and not bundle.agent.queries and not bundle.probes and bundle.search is None:return []
            value=bundle.executed_request
            if (value is None or value.kind!='search' or value.source_id!='tme-campus' or value.expected_version!=run['goal_version']
                or not bundle.agent.queries or bundle.agent.queries[-1]!=value):raise ValueError('delegated_scope_mismatch')
            ids={p.job_id for p in bundle.probes if p.job}
            if not set(bundle.agent.read_job_ids).issubset(ids):raise ValueError('invented_inspected_job')
            if bundle.agent.action=='finish' and ids and not bundle.agent.read_job_ids:raise ValueError('inspection_required')
        elif bundle.agent is not None or bundle.executed_request is not None:raise ValueError('unexpected_agent_report')
        return checked_items(value,bundle,payload['jd_text'],payload['conditions'])

    async def cached(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise ValueError('research_claim_expired')
            return rows[1]['candidate']

    async def save(self,claim,bundle,agent_origin=None):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            payload=rows[1]['input_payload']
            self.items(rows[1],bundle)
            await conn.execute('UPDATE app.career_research_runs SET candidate=%s,agent_origin=%s WHERE id=%s',(Jsonb(bundle.model_dump(mode='json')),agent_origin,claim.message.operation_id))
            return True

    async def complete(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            goal,run=rows;payload=run['input_payload']
            bundle=ResearchBundle.model_validate_json(json.dumps(run['candidate']))
            items=self.items(run,bundle);matches=[]
            for item in items:
                params=(goal['id'],item['source_id'],item['source_job_id'],item['content_sha256'])
                position_id=await self._position_for(conn,goal['id'],item['source_id'],item['source_job_id'])
                snapshot=await (await conn.execute('''INSERT INTO app.career_job_snapshots(goal_id,source_id,source_job_id,content_sha256,source_url,retrieval_url,payload,position_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(goal_id,source_id,source_job_id,content_sha256) DO NOTHING RETURNING id''',
                    (*params,item['source_url'],item['retrieval_url'],Jsonb(item['payload']),position_id))).fetchone()
                if not snapshot:
                    snapshot=await (await conn.execute('SELECT id FROM app.career_job_snapshots WHERE goal_id=%s AND source_id=%s AND source_job_id=%s AND content_sha256=%s',params)).fetchone()
                await self._point_at_snapshot(conn,position_id,snapshot['id'])
                await conn.execute('''INSERT INTO app.career_job_observations(run_id,snapshot_id,observed_at,response_sha256,city_check,included)
                    VALUES(%s,%s,%s,%s,%s,%s)''',(run['id'],snapshot['id'],item['observed_at'],item['response_sha256'],item['city_check'],item['included']))
                matches.append({'snapshot_id':str(snapshot['id']),**checks(item['payload'],payload['conditions'],run['goal_version']),
                                'agent_reviewed':bundle.agent is not None and item['source_job_id'] in bundle.agent.read_job_ids})
            failures=[{'job_id':p.job_id,'status':p.status} for p in bundle.probes if p.status!='detail_readable']
            if bundle.search and bundle.search.status!='ok':failures.append({'status':bundle.search.status})
            if bundle.error_code:failures.append({'status':bundle.error_code})
            status=('PARTIAL' if items else 'FAILED') if failures else 'SUCCEEDED'
            result={'origin':bundle.origin,'search':bundle.search.model_dump(mode='json') if bundle.search else None,
                    'readable':len(items),'included':sum(x['included'] for x in items),'failures':failures,
                    'coverage':'keyword_first_page_max_5' if payload['request']['kind']=='search' else payload['request']['kind'],
                    'availability':'unknown','matches':matches,'agent':bundle.agent.model_dump(mode='json') if bundle.agent else None,
                    'executed_request':bundle.executed_request.model_dump(mode='json') if bundle.executed_request else (None if run['mode']=='agent' else payload['request'])}
            await conn.execute('UPDATE app.career_research_runs SET status=%s,lease_until=NULL,result=%s,revision=revision+1 WHERE id=%s',(status,Jsonb(result),run['id']))
            if run['mode']=='agent':
                from .handoffs import continue_after
                resumed=await continue_after(conn,goal,payload,'research',run['id'],status)
                if not resumed and goal['status']=='WAITING_FOR_TOOL':
                    latest=await (await conn.execute('SELECT id FROM app.career_steps WHERE goal_id=%s AND goal_version=%s ORDER BY cycle DESC LIMIT 1',(goal['id'],goal['version']))).fetchone()
                    if latest and latest['id']==run['parent_step_id']:
                        await conn.execute("UPDATE app.career_goals SET status='WAITING_FOR_USER',revision=revision+1 WHERE id=%s",(goal['id'],))
            return True

    async def fail_assessment(self,claim,**kwargs):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            state='WAITING_CREDENTIALS' if kwargs.get('waiting') else 'FAILED'
            await conn.execute('UPDATE app.career_research_runs SET status=%s,lease_until=NULL,revision=revision+1,error_code=%s WHERE id=%s',(state,kwargs.get('code','research_failed'),claim.message.operation_id))
            if kwargs.get('waiting'):
                from interview_agent.career.handoffs import pause_tool_parent
                await pause_tool_parent(conn,rows[0]['id'],rows[1]['input_payload'],'admission_wait','research')
            if state=='FAILED' and rows[1]['mode']=='agent':
                from .handoffs import continue_after
                resumed=await continue_after(conn,rows[0],rows[1]['input_payload'],'research',rows[1]['id'],state)
                if not resumed:
                    from interview_agent.career.handoffs import pause_tool_parent
                    await pause_tool_parent(conn,rows[0]['id'],rows[1]['input_payload'],'task_failed','research')
            return True

    async def fail_attempt(self,claim):return await self.fail_assessment(claim)

    async def can_ack(self,message):
        async with self.pool.connection() as conn:
            row=await (await conn.execute('''SELECT r.status,r.goal_version,g.version,g.status AS goal_status FROM app.career_research_runs r
                JOIN app.career_goals g ON g.id=r.goal_id WHERE r.id=%s AND r.job_id=%s''',(message.operation_id,message.job_id))).fetchone()
            return not row or row['status'] in TERMINAL or row['goal_version']!=row['version'] or row['goal_status']=='CANCELLED'

    async def clear_selection(self,principal,identity,version):
        """F07Z-166 D1: delete this version's selection; the frozen plan is unaffected."""
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
            if goal['version']!=version or goal['status']=='CANCELLED':
                raise ApiError('version_conflict',409)
            await conn.execute('DELETE FROM app.career_job_selections WHERE goal_id=%s AND goal_version=%s',
                               (identity,version))
        return {'goal_version':version,'cleared':True}

    async def select(self,principal,identity,value):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            goal=await CareerRepository(self.pool)._goal(conn,principal.user_id,identity,True)
            if goal['version']!=value.expected_version or goal['status']=='CANCELLED':raise ApiError('version_conflict',409)
            row=await (await conn.execute('''SELECT s.payload,r.input_payload FROM app.career_research_runs r
                JOIN app.career_job_observations o ON o.run_id=r.id JOIN app.career_job_snapshots s ON s.id=o.snapshot_id
                WHERE r.id=%s AND r.goal_id=%s AND r.goal_version=%s AND s.id=%s AND s.goal_id=%s AND r.status IN ('SUCCEEDED','PARTIAL')''',
                (value.run_id,identity,goal['version'],value.snapshot_id,identity))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            match=checks(row['payload'],row['input_payload']['conditions'],goal['version'])
            if match['outcome']=='excluded':raise ApiError('hard_requirement_conflict',409)
            if match['outcome']=='needs_verification' and not value.acknowledge_unknown:raise ApiError('unknown_acknowledgement_required',409)
            existing=await (await conn.execute('SELECT run_id,snapshot_id FROM app.career_job_selections WHERE goal_id=%s AND goal_version=%s',(identity,goal['version']))).fetchone()
            if existing:
                if str(existing['run_id'])!=value.run_id or str(existing['snapshot_id'])!=value.snapshot_id:raise ApiError('selection_already_confirmed',409)
                return
            if value.continue_on_selection and goal['status'] in ('PENDING','RUNNING','WAITING_CREDENTIALS'):raise ApiError('coordination_in_progress',409)
            selection=await (await conn.execute('''INSERT INTO app.career_job_selections(goal_id,goal_version,run_id,snapshot_id,acknowledged_unknown)
                VALUES(%s,%s,%s,%s,%s) RETURNING id''',(identity,goal['version'],value.run_id,value.snapshot_id,value.acknowledge_unknown))).fetchone()
            position=await (await conn.execute('SELECT position_id FROM app.career_job_snapshots WHERE id=%s',(value.snapshot_id,))).fetchone()
            if position and position['position_id']:
                from .bindings import BindingRepository
                await BindingRepository(self.pool).auto_bind(conn,identity,position['position_id'])
            if value.continue_on_selection:
                from .handoffs import continue_user_event
                await continue_user_event(conn,goal,'job_selected',selection['id'])
