"""Goal-locked application history. Packages/events are append-only; reports are user claims."""
import hashlib
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.career.repository import CareerRepository,digest
from interview_agent.materials.repository import MaterialRepository
from .contracts import transition
from .planner import review

class ApplicationRepository:
    def __init__(self,pool): self.pool=pool

    async def _root(self,conn,user,gid,aid=None):
        await require_session(conn,user)
        g=await CareerRepository(self.pool)._goal(conn,user.user_id,gid,True)
        m=await MaterialRepository(self.pool)._state(conn,gid)
        a=None
        if aid:
            a=await (await conn.execute('SELECT * FROM app.career_applications WHERE id=%s AND goal_id=%s FOR UPDATE',(aid,gid))).fetchone()
            if not a: raise ApiError('resource_unavailable',404)
        return g,m,a

    async def _package(self,conn,g,m,v):
        if g['version']!=v.expected_version or g['status']=='CANCELLED': raise ApiError('version_conflict',409)
        if m['revision']!=v.expected_material_revision: raise ApiError('material_revision_conflict',409)
        target=await MaterialRepository(self.pool)._target(conn,g)
        if target['kind']=='direction': raise ApiError('application_job_required',409)
        # F07Z-152: default to the selected position's bound resume when the caller omits one.
        # A bound draft was authored for whatever target existed then; the binding is the user's
        # decision to use it for this position, so only an explicitly chosen draft must still match
        # the current target (same rule as the interview default).
        bound_default=False
        if not v.draft_id:
            bound=await (await conn.execute('''SELECT b.resume_draft_id FROM app.career_job_selections sel
                JOIN app.career_job_snapshots s ON s.id=sel.snapshot_id
                JOIN app.career_position_bindings b ON b.position_id=s.position_id AND b.active
                WHERE sel.goal_id=%s AND sel.goal_version=%s''',(g['id'],g['version']))).fetchone()
            if bound:
                v=v.model_copy(update={'draft_id':str(bound['resume_draft_id'])})
                bound_default=True
        material={'kind':'none','facts':[]}
        if v.draft_id:
            d=await (await conn.execute('SELECT * FROM app.career_material_drafts WHERE id=%s AND goal_id=%s',(v.draft_id,g['id']))).fetchone()
            if not d: raise ApiError('resource_unavailable',404)
            if d['goal_version']!=g['version'] or d['material_revision']!=m['revision'] or (not bound_default and digest(d['payload']['target'])!=digest(target)): raise ApiError('draft_stale',409)
            from interview_agent.materials.usable import usable_markdown
            body=usable_markdown(d['payload'],fallback=d['markdown'])
            material={'kind':'draft','id':str(d['id']),'version':d['version'],'markdown':body,'facts':d['payload'].get('facts') or [],
                      'content_sha256':hashlib.sha256(body.encode()).hexdigest()}
            from interview_agent.materials.resume_evidence import resume_evidence
            selected=resume_evidence(str(d['id']),d['version'],d['payload'])
            if selected:material['resume_evidence']=selected
        elif v.source_id:
            s=await (await conn.execute('SELECT * FROM app.career_material_sources WHERE id=%s AND goal_id=%s',(v.source_id,g['id']))).fetchone()
            if not s: raise ApiError('resource_unavailable',404)
            facts=await (await conn.execute('SELECT id,source_id,kind,quote FROM app.career_material_facts WHERE goal_id=%s AND source_id=%s ORDER BY created_at,id',(g['id'],v.source_id))).fetchall()
            material={'kind':'source','id':str(s['id']),'text':s['body'],'content_sha256':s['content_sha256'],
                      'facts':[{**f,'id':str(f['id']),'source_id':str(f['source_id'])} for f in facts]}
        for f in material['facts']: f['verification']='user_confirmed_not_external_verified'
        return {'goal_version':g['version'],'material_revision':m['revision'],'target':target,
                'company':v.company,'title':v.title,'entry_url':v.application_url or target.get('source_url') or '',
                'material':material,'verification':'user_prepared_not_externally_submitted'}

    async def _prior(self,conn,aid,key,body):
        e=await (await conn.execute('SELECT request_hash FROM app.career_application_events WHERE application_id=%s AND request_id=%s',(aid,key))).fetchone()
        if e and e['request_hash']!=digest(body): raise ApiError('idempotency_conflict',409)
        return bool(e)

    async def _event(self,conn,a,key,body,kind,payload,revision=None):
        n=(await (await conn.execute('SELECT count(*) AS n FROM app.career_application_events WHERE application_id=%s',(a['id'],))).fetchone())['n']
        if n>=200: raise ApiError('application_event_limit',409)
        await conn.execute('INSERT INTO app.career_application_events(application_id,seq,package_revision,request_id,request_hash,kind,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                           (a['id'],n+1,revision or a['package_revision'],key,digest(body),kind,Jsonb(payload)))

    async def create(self,user,gid,key,v):
        body=v.model_dump(mode='json')
        async with self.pool.connection() as conn,conn.transaction():
            g,m,_=await self._root(conn,user,gid)
            old=await (await conn.execute('SELECT id,request_hash FROM app.career_applications WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
            if old:
                if old['request_hash']!=digest(body): raise ApiError('idempotency_conflict',409)
                return str(old['id']),False
            p=await self._package(conn,g,m,v)
            if p['target']['kind']=='selected_job':
                origin=await (await conn.execute('SELECT source_id,source_job_id FROM app.career_job_snapshots WHERE id=%s AND goal_id=%s',(p['target']['snapshot_id'],gid))).fetchone()
                job_key=digest(dict(origin))
            else:
                job_key=digest({'jd':p['target']['content_sha256'],'company':v.company,'title':v.title})
            # Deduplicate the same prepared job even if a client uses a new request key.
            if await (await conn.execute('SELECT id FROM app.career_applications WHERE goal_id=%s AND job_key=%s',(gid,job_key))).fetchone(): raise ApiError('application_already_exists',409)
            count=(await (await conn.execute('SELECT count(*) AS n FROM app.career_applications WHERE goal_id=%s',(gid,))).fetchone())['n']
            if count>=30: raise ApiError('application_limit',409)
            a=await (await conn.execute('INSERT INTO app.career_applications(goal_id,request_id,request_hash,job_key) VALUES(%s,%s,%s,%s) RETURNING *',(gid,key,digest(body),job_key))).fetchone()
            await conn.execute('INSERT INTO app.career_application_packages(application_id,revision,payload,sha256) VALUES(%s,1,%s,%s)',(a['id'],Jsonb(p),digest(p)))
            return str(a['id']),True

    async def command(self,user,gid,aid,key,v,kind):
        body={'kind':kind,**v.model_dump(mode='json')}
        async with self.pool.connection() as conn,conn.transaction():
            g,m,a=await self._root(conn,user,gid,aid)
            if await self._prior(conn,aid,key,body): return False
            if a['version']!=v.expected_application_version: raise ApiError('application_version_conflict',409)
            if kind=='package':
                if a['status']!='DRAFT': raise ApiError('application_package_frozen',409)
                rev=a['package_revision']+1
                if rev>30: raise ApiError('application_package_limit',409)
                p=await self._package(conn,g,m,v)
                # Package revisions may update materials/entry, but not silently switch jobs.
                old=await self.training_context(conn,gid,aid,a['package_revision'])
                if digest(p['target'])!=digest(old['package']['target']) or (p['company'],p['title'])!=(old['package']['company'],old['package']['title']): raise ApiError('application_job_changed',409)
                await conn.execute('INSERT INTO app.career_application_packages(application_id,revision,payload,sha256) VALUES(%s,%s,%s,%s)',(aid,rev,Jsonb(p),digest(p)))
                await self._event(conn,a,key,body,kind,{'package_revision':rev},rev)
                await conn.execute('UPDATE app.career_applications SET package_revision=%s,version=version+1 WHERE id=%s',(rev,aid))
            elif kind=='progress':
                try: transition(a['status'],v.status,v.is_correction)
                except ValueError: raise ApiError('application_transition_invalid',409) from None
                await self._event(conn,a,key,body,kind,{**v.model_dump(mode='json'),'verification':'user_reported_not_external_verified'})
                await conn.execute('UPDATE app.career_applications SET status=%s,version=version+1,submitted_package_revision=CASE WHEN %s THEN package_revision ELSE submitted_package_revision END WHERE id=%s',
                                   (v.status,v.status=='SUBMITTED',aid))
            else:
                await self._event(conn,a,key,body,kind,{**v.model_dump(mode='json'),'verification':'user_reported_not_external_verified'})
                await conn.execute('UPDATE app.career_applications SET version=version+1 WHERE id=%s',(aid,))
            return True

    async def training_context(self,conn,gid,aid,package_revision=None):
        a=await (await conn.execute('SELECT * FROM app.career_applications WHERE id=%s AND goal_id=%s',(aid,gid))).fetchone()
        if not a: raise ApiError('resource_unavailable',404)
        rev=package_revision or a['submitted_package_revision'] or a['package_revision']
        p=await (await conn.execute('SELECT * FROM app.career_application_packages WHERE application_id=%s AND revision=%s',(aid,rev))).fetchone()
        if not p: raise ApiError('resource_unavailable',404)
        return {'id':str(a['id']),'version':a['version'],'package_revision':rev,'package_sha256':p['sha256'],'package':p['payload']}

    async def get(self,user,gid,aid):
        async with self.pool.connection() as conn,conn.transaction():
            g,m,a=await self._root(conn,user,gid,aid)
            return await self.view_at(conn,g,m,a)

    async def view_at(self,conn,g,m,a):
        """Internal caller must already own/lock the goal; public get authenticates first."""
        gid,aid=g['id'],str(a['id'])
        p=await self.training_context(conn,gid,aid,a['package_revision'])
        rows=await (await conn.execute('SELECT id,kind,payload,package_revision,created_at FROM app.career_application_events WHERE application_id=%s ORDER BY seq',(aid,))).fetchall()
        events=[{**e,'id':str(e['id'])} for e in rows]
        packages=await (await conn.execute('SELECT revision,sha256,payload FROM app.career_application_packages WHERE application_id=%s ORDER BY revision',(aid,))).fetchall()
        sessions=await (await conn.execute("""SELECT id,status,input_payload->'application' AS binding,
            input_payload#>>'{policy,role}' AS role,input_payload#>>'{policy,mode}' AS mode
            FROM app.career_interviews WHERE goal_id=%s AND input_payload#>>'{application,id}'=%s ORDER BY created_at,id""",(gid,aid))).fetchall()
        observations=await (await conn.execute("""SELECT o.id,o.session_id,o.answer_id,o.payload,s.input_payload->'application' AS binding
            FROM app.career_interview_observations o JOIN app.career_interviews s ON s.id=o.session_id
            WHERE s.goal_id=%s AND s.input_payload#>>'{application,id}'=%s AND s.status='COMPLETED'
            AND NOT EXISTS(SELECT 1 FROM app.career_interview_corrections c WHERE c.observation_id=o.id)
            ORDER BY o.created_at,o.id""",(gid,aid))).fetchall()
        training=[{**o,'id':str(o['id']),'session_id':str(o['session_id']),'answer_id':str(o['answer_id'])} for o in observations]
        from interview_agent.written.memory import evidence_at
        written=await evidence_at(conn,gid,aid)
        return {'id':aid,'version':a['version'],'status':a['status'],'current_package_revision':a['package_revision'],
                'submitted_package_revision':a['submitted_package_revision'],'package':p['package'],'packages':packages,'events':events,
                'training_sessions':[{**s,'id':str(s['id'])} for s in sessions],'training_observations':training,'written_observations':written,
                'review':review(p['package'],events,{'goal_version':g['version'],'material_revision':m['revision'],
                    'goal_cancelled':g['status']=='CANCELLED','status':a['status'],'package_revision':a['package_revision'],'package_sha256':p['package_sha256']},training,written)}

    async def summaries_at(self,conn,gid):
        rows=await (await conn.execute('''SELECT a.id,a.version,a.status,p.payload->>'company' AS company,p.payload->>'title' AS title
            FROM app.career_applications a JOIN app.career_application_packages p ON p.application_id=a.id AND p.revision=a.package_revision
            WHERE a.goal_id=%s ORDER BY a.created_at,a.id LIMIT 30''',(gid,))).fetchall()
        return [{**r,'id':str(r['id'])} for r in rows]

    async def coordinator_context_at(self,conn,g,aid):
        """Read-only tool projection under the caller's goal lock; no raw resume/JD export."""
        a=await (await conn.execute('SELECT * FROM app.career_applications WHERE id=%s AND goal_id=%s',(aid,g['id']))).fetchone()
        if not a:raise ApiError('resource_unavailable',404)
        m=await MaterialRepository(self.pool)._state(conn,g['id']);v=await self.view_at(conn,g,m,a)
        reports=[{'id':e['id'],'kind':e['kind'],'payload':e['payload']} for kind in ('progress','availability')
                 for e in [next((e for e in reversed(v['events']) if e['kind']==kind),None)] if e]
        value={'application_id':aid,'application_version':a['version'],'status':a['status'],
               'goal_version':g['version'],'material_revision':m['revision'],
               'company':v['package']['company'],'title':v['package']['title'],
               'package_revision':a['package_revision'],'package_sha256':v['review']['package_sha256'],
               'submitted_package_revision':a['submitted_package_revision'],
               'material_kind':v['package']['material']['kind'],'facts':v['review']['facts'],'warnings':v['review']['warnings'],
               'reports':reports,'training_evidence':v['training_observations'],'written_evidence':v['written_observations'],
               'actions':[{'id':digest(action),**action} for action in v['review']['actions']]}
        # Hash all eligible evidence before limiting the model view; old evidence changes also invalidate plans.
        sha=digest(value);shown=value['training_evidence'][-6:];shown_ids={o['id'] for o in shown}
        actions=[x for x in value['actions'] if x['kind']!='retest_topic' or any('observation:'+i in x['evidence_refs'] for i in shown_ids)]
        written_shown=value['written_evidence'][-6:];written_ids={o['id'] for o in written_shown}
        actions=[x for x in actions if x['kind'] not in ('retest_written','review_written') or any('written-observation:'+i in x['evidence_refs'] for i in written_ids)]
        return {**value,'context_sha256':sha,'training_evidence':shown,'written_evidence':written_shown,'actions':actions,
                'written_evidence_total':len(value['written_evidence']),'written_evidence_omitted':len(value['written_evidence'])-len(written_shown),
                'training_evidence_total':len(value['training_evidence']),'training_evidence_omitted':len(value['training_evidence'])-len(shown)}

    async def listing(self,user,gid):
        async with self.pool.connection() as conn,conn.transaction():
            await self._root(conn,user,gid)
            rows=await (await conn.execute('SELECT id,status,version FROM app.career_applications WHERE goal_id=%s ORDER BY created_at,id',(gid,))).fetchall()
            return {'items':[{**x,'id':str(x['id'])} for x in rows]}
