import json
"""Whole-session SQL state, immutable events and source-bound observations."""
from interview_agent import observability
from interview_agent.career.budget import legacy_budget_exhausted
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim,duration
from interview_agent.career.repository import CareerRepository,digest
from interview_agent.materials.repository import MaterialRepository
from interview_agent.applications.repository import ApplicationRepository
from .catalog import pack,topic
from .retest import suggestions as suggest_retest
from .contracts import validate_decision,question_payload,project_summary_enabled,PROJECT_SUMMARY_VERSION
from interview_agent.materials.resume_evidence import resume_evidence,select_resume_evidence

ACTIVE={'PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS'}
RUN_TERMINAL={'SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS'}


def _bank_declaration(run):
    """Decision.bank_use of the latest run, tolerant of how the JSONB column comes back."""
    if not run:
        return None
    raw = run['result']
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw.get('bank_use') if isinstance(raw, dict) else None


class InterviewRepository:
    def __init__(self,pool,tracer=None,projector=None):
        self.pool=pool;self.tracer=tracer if tracer is not None else observability.current()
        # Optional, best-effort Store projection of finished sessions (S02). Off unless the
        # running Worker constructs a projector, so tests and one-off scripts stay SQL-only.
        self.projector=projector

    async def _memory_state(self,conn,gid):
        await conn.execute('INSERT INTO app.career_interview_memory_state(goal_id) VALUES(%s) ON CONFLICT DO NOTHING',(gid,))
        return await (await conn.execute('SELECT revision FROM app.career_interview_memory_state WHERE goal_id=%s FOR UPDATE',(gid,))).fetchone()

    async def _root(self,conn,gid,user=None):
        goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s'+(' AND user_id=%s' if user else '')+' FOR UPDATE',(gid,user) if user else (gid,))).fetchone()
        if not goal:raise ApiError('resource_unavailable',404)
        material=await MaterialRepository(self.pool)._state(conn,gid);memory=await self._memory_state(conn,gid)
        return goal,material,memory

    async def _fresh(self,conn,goal,material,memory,s):
        return (goal['status']!='CANCELLED' and goal['version']==s['goal_version'] and memory['revision']==s['memory_revision']
                and (not s['input_payload'].get('material_alignment') or material['revision']==s['material_revision'])
                and (bool(s['input_payload'].get('application')) or (
                    (not s['input_payload']['facts'] or material['revision']==s['material_revision'])
                    and digest(await MaterialRepository(self.pool)._target(conn,goal))==digest(s['input_payload']['target']))))

    async def _end(self,conn,s,status,reason,summary=None):
        await conn.execute('UPDATE app.career_interviews SET status=%s,stop_reason=%s,model_summary=%s,version=version+1,revision=revision+1 WHERE id=%s',(status,reason,summary,s['id']))
        await conn.execute("""UPDATE app.career_interview_runs SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1,revision=revision+1,error_code=%s
            WHERE session_id=%s AND status IN ('PENDING','RUNNING','WAITING_CREDENTIALS')""",(reason,s['id']))
        from interview_agent.career.handoffs import pause_tool_parent
        await pause_tool_parent(conn,s['goal_id'],s['input_payload'],reason,'interview')

    async def _refresh(self,conn,root,s):
        if s['status'] in ACTIVE:
            now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            from interview_agent.interview_wiki.repository import usable
            wiki_id = s['input_payload'].get('wiki_version_id')
            if wiki_id and not await usable(conn, root[0]['user_id'], wiki_id):
                await self._end(conn, s, 'STALE', 'wiki_source_withdrawn')
            elif not await self._fresh(conn,*root,s):await self._end(conn,s,'STALE','context_changed')
            elif s['deadline_at']<=now:await self._end(conn,s,'COMPLETED','deadline')
            else:return s
            return await (await conn.execute('SELECT * FROM app.career_interviews WHERE id=%s',(s['id'],))).fetchone()
        return s

    async def _session(self,conn,gid,sid,user=None):
        root=await self._root(conn,gid,user)
        s=await (await conn.execute('SELECT * FROM app.career_interviews WHERE id=%s AND goal_id=%s FOR UPDATE',(sid,gid))).fetchone()
        if not s:raise ApiError('resource_unavailable',404)
        return root,await self._refresh(conn,root,s)

    async def _scope(self,conn,s):
        events=await (await conn.execute('SELECT id,kind,payload FROM app.career_interview_events WHERE session_id=%s ORDER BY seq',(s['id'],))).fetchall()
        questions=[{'id':str(e['id']),**e['payload']} for e in events if e['kind']=='question']
        # A clarification event may follow a question/skip; it never means "no answer". Find the
        # latest answer-or-skip event instead of assuming the last event is one of them.
        answer_event=next((e for e in reversed(events) if e['kind'] in ('answer','skip')),None)
        answer={'id':str(answer_event['id']),**answer_event['payload']} if answer_event and answer_event['kind']=='answer' else None
        own=await (await conn.execute('SELECT id,payload FROM app.career_interview_observations WHERE session_id=%s ORDER BY created_at DESC LIMIT 6',(s['id'],))).fetchall()
        prior=s['input_payload']['prior_observations']
        skipped=[str(e['payload'].get('question_id') or '') for e in events if e['kind']=='skip']
        answers=[{'id':str(e['id']),'question_id':str(e['payload'].get('question_id') or ''),
                  'topic_id':e['payload'].get('topic_id') or '','text':e['payload'].get('text') or '',
                  'assisted':bool(e['payload'].get('assisted'))} for e in events if e['kind']=='answer']
        return {**s['input_payload'],'question_history':questions,'current_question':questions[-1] if questions else None,'answer':answer,
                # F07Z-167: a skipped round still counts as asked; it just has no answer to score.
                'skipped_question_ids':skipped,'current_skipped':bool(answer_event and answer_event['kind']=='skip'),
                # F07Z-167 Phase 2: the full original answers stay in the internal snapshot so the
                # model can look one up by id after it left the recent window; the model view only
                # carries bounded anchors.
                'answers':answers,
                'assisted_topics':list(dict.fromkeys(e['payload']['topic_id'] for e in events if e['kind']=='explanation')),
                'prior_observations':prior+[{'id':str(o['id']),'source_session_id':str(s['id']),'answer_id':o['payload']['answer_id'],
                                            'answer_quote':o['payload']['answer_quote'],'pack_version':o['payload']['pack_version'],
                                            'topic_id':o['payload']['topic_id'],'label':o['payload']['label'],'summary':o['payload']['summary'],
                                            **({'summary_origin':o['payload']['summary_origin']} if 'summary_origin' in o['payload'] else {}),
                                            'application':s['input_payload'].get('application'),'origin':'this_session_observation'} for o in reversed(own)]}

    async def _enqueue(self,conn,s):
        scope=await self._scope(conn,s)
        remaining=await (await conn.execute(
            "SELECT greatest(0,extract(epoch FROM deadline_at-clock_timestamp()))::int AS n "
            "FROM app.career_interviews WHERE id=%s",(s['id'],))).fetchone()
        scope['remaining_seconds']=int(remaining['n'])
        row=await (await conn.execute('''INSERT INTO app.career_interview_runs(goal_id,goal_version,session_id,session_version,input_payload)
            VALUES(%s,%s,%s,%s,%s) RETURNING id''',(s['goal_id'],s['goal_version'],s['id'],s['version'],Jsonb(scope)))).fetchone()
        return str(row['id'])

    async def create(self,principal,gid,key,value,*,confirmation_step=None):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal)
            return await self._create(conn,principal.user_id,gid,key,value,confirmation_step)

    async def create_for_owner(self,user_id,gid,key,value,*,generated_questions=(),generated_target_sha256=None,
                               jd_coverage=()):
        """Worker-only path: the caller already proved ownership (preparation row + goal lock).

        `_root` still filters the goal by `user_id`, so a wrong owner gets 404 like the API.
        """
        async with self.pool.connection() as conn,conn.transaction():
            return await self._create(conn,user_id,gid,key,value,None,
                                      generated_questions=generated_questions,
                                      generated_target_sha256=generated_target_sha256,jd_coverage=jd_coverage)

    async def _create(self,conn,user_id,gid,key,value,confirmation_step,*,generated_questions=(),
                      generated_target_sha256=None,jd_coverage=()):
        if confirmation_step:
            from uuid import UUID
            confirmation_step=str(UUID(confirmation_step))
        goal,material,memory=await self._root(conn,gid,user_id)
        if value.role is None:
            from interview_agent.career.roles import resolve_role
            selected=await (await conn.execute('''SELECT s.payload->>'title' AS title
                FROM app.career_job_selections sel JOIN app.career_job_snapshots s ON s.id=sel.snapshot_id
                WHERE sel.goal_id=%s AND sel.goal_version=%s''',(gid,goal['version']))).fetchone()
            role=resolve_role(direction=(goal['conditions'] or {}).get('direction') or '',
                              title=(selected or {}).get('title') or '',message='')
            if role is None:raise ApiError('role_required',409)
            value=value.model_copy(update={'role':role})
        # F07Z-152: a selected position's bound resume is the default for a new session. An explicit
        # draft_id still wins, and an application package keeps its own frozen material.
        bound_default=False
        if not value.draft_id and not value.application_id and value.use_resume:
            bound=await (await conn.execute('''SELECT b.resume_draft_id FROM app.career_job_selections sel
                JOIN app.career_job_snapshots s ON s.id=sel.snapshot_id
                JOIN app.career_position_bindings b ON b.position_id=s.position_id AND b.active
                WHERE sel.goal_id=%s AND sel.goal_version=%s''',(gid,goal['version']))).fetchone()
            if bound:
                value=value.model_copy(update={'draft_id':str(bound['resume_draft_id'])})
                bound_default=True
        body=value.model_dump(mode='json')
        if value.profile is None:body.pop('profile',None)
        # F07Z-167: the full profile is server-configured; everything below freezes these values.
        from .simulation import FULL_SETUP_GRACE_SECONDS, capacity, minimum_questions, resolved
        max_questions,duration_seconds=resolved(value.profile,value.max_questions,value.duration_seconds)
        # Keep the hash of requests created before this field existed.
        if value.use_resume:body.pop('use_resume',None)
        if not value.resume_item_indexes:body.pop('resume_item_indexes')
        if not value.question_banks:body.pop('question_banks')
        if value.allow_project_questions is None:body.pop('allow_project_questions')
        if value.alignment_run_id is None:body.pop('alignment_run_id')
        if not value.include_practice_mistakes:body.pop('include_practice_mistakes')
        if value.focus_session_id is None:body.pop('focus_session_id')
        if value.focus_observation_id is None:body.pop('focus_observation_id')
        if body.get('continue_on_completion') is False:body.pop('continue_on_completion')
        # Preserve hashes of requests created before application binding existed.
        if not value.application_id:
            for field in ('application_id','application_package_revision','interview_round'):body.pop(field)
        if confirmation_step:body['coordinator_step_id']=confirmation_step
        if generated_questions:
            body['generated_questions']=generated_questions
        if jd_coverage:
            body['jd_coverage']=jd_coverage
        if generated_questions or jd_coverage:
            body['generated_target_sha256']=generated_target_sha256
        hashed=digest(body)
        prior=await (await conn.execute('SELECT id,request_hash FROM app.career_interviews WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
        if prior:
            if prior['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
            return str(prior['id']),False
        if goal['version']!=value.expected_version or goal['status']=='CANCELLED':raise ApiError('version_conflict',409)
        handoff=None
        if confirmation_step:
            from interview_agent.career.handoffs import confirm_step
            handoff=await confirm_step(conn,goal,confirmation_step,'prepare_training','career_interviews')
            handoff['continue_on_completion']=getattr(value,'continue_on_completion',False)
        count=await (await conn.execute('SELECT count(*) AS n FROM app.career_interviews WHERE goal_id=%s',(gid,))).fetchone()
        if count['n']>=20:raise ApiError('interview_session_limit',409)
        old=await (await conn.execute("SELECT * FROM app.career_interviews WHERE goal_id=%s AND status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS') ORDER BY created_at FOR UPDATE",(gid,))).fetchall()
        for s in old:
            if (await self._refresh(conn,(goal,material,memory),s))['status'] in ACTIVE:raise ApiError('interview_in_progress',409)
        facts=[];ids=value.fact_ids;application=None;draft_id=value.draft_id
        if value.application_id:
            context=await ApplicationRepository(self.pool).training_context(conn,gid,value.application_id,value.application_package_revision)
            saved=context['package'];target=saved['target'];frozen=saved['material']['facts']
            candidates=saved['material'].get('resume_evidence',[])
            if candidates or value.resume_item_indexes:frozen=select_resume_evidence(candidates,value.resume_item_indexes)
            if ids and not set(ids)<={f['id'] for f in frozen}:raise ApiError('fact_not_in_application_package',400)
            facts=[f for f in frozen if not ids or f['id'] in ids]
            if len(facts)>6:raise ApiError('material_scope_too_large_choose_fact_ids',400)
            draft_id=saved['material']['id'] if saved['material']['kind']=='draft' else None
            application={k:context[k] for k in ('id','package_revision','package_sha256')}
            application.update(interview_round=value.interview_round,company=saved['company'],title=saved['title'],
                               package_goal_version=saved['goal_version'],package_material_revision=saved['material_revision'])
        else:target=await MaterialRepository(self.pool)._target(conn,goal)
        if (generated_questions or jd_coverage) and digest(target)!=generated_target_sha256:
            raise ApiError('context_changed',409)
        wiki_id = next((item['wiki']['id'] for item in generated_questions if item.get('wiki')), None)
        if wiki_id:
            from interview_agent.interview_wiki.repository import usable
            if not await usable(conn, user_id, wiki_id):
                raise ApiError('wiki_source_withdrawn', 409)
        if value.draft_id:
            if ids:raise ApiError('choose_draft_or_facts',400)
            d=await (await conn.execute('SELECT * FROM app.career_material_drafts WHERE id=%s AND goal_id=%s',(value.draft_id,gid))).fetchone()
            if not d:raise ApiError('resource_unavailable',404)
            if d['goal_version']!=goal['version'] or d['material_revision']!=material['revision'] or (not bound_default and digest(d['payload']['target'])!=digest(target)):raise ApiError('draft_stale',409)
            candidates=resume_evidence(str(d['id']),d['version'],d['payload'])
            # An uploaded resume has no item picker in the setup form. Keep the same six-anchor
            # ceiling by taking the first six lines instead of rejecting the session.
            if candidates and not value.resume_item_indexes and d['payload'].get('draft_style')=='verbatim':
                candidates=candidates[:6]
            if candidates or value.resume_item_indexes:facts=select_resume_evidence(candidates,value.resume_item_indexes)
            ids=[f['id'] for f in (d['payload'].get('facts') or [])]
            if len(ids)>6:raise ApiError('material_scope_too_large_choose_fact_ids',400)
        if ids and not value.application_id:
            rows=await (await conn.execute('SELECT id,source_id,kind,quote FROM app.career_material_facts WHERE goal_id=%s AND id=ANY(%s::uuid[]) ORDER BY created_at,id',(gid,ids))).fetchall()
            if len(rows)!=len(ids):raise ApiError('resource_unavailable',404)
            facts=[{**r,'id':str(r['id']),'source_id':str(r['source_id']),'verification':'user_confirmed_not_external_verified'} for r in rows]
        focus=None
        if value.focus_observation_id:
            from .focus import resolve as resolve_focus
            focus=await resolve_focus(conn,gid,value.focus_session_id,value.focus_observation_id)
        from interview_agent.question_banks.repository import BankRepository
        selected_pack,selected_banks=await BankRepository(self.pool).resolve(conn,user_id,value.role,value.question_banks)
        # 新增业务 1: a private bank augments the role's own pack, it does not replace it. A thin
        # bank must never be the reason a session has nothing to ask; the user's items simply come
        # first (which is how they get more weight without a separate scoring pass).
        bank_topics = 0
        if value.question_banks:
            base_pack,_=await BankRepository(self.pool).resolve(conn,user_id,value.role,[])
            bank_topics=len(selected_pack['topics'])
            merged={**selected_pack['topics'],**base_pack['topics']}
            order=[*selected_pack['topic_order'],
                   *[topic_id for topic_id in base_pack['topic_order'] if topic_id not in selected_pack['topics']]]
            selected_pack={**selected_pack,'topics':merged,'topic_order':order}
        if generated_questions:
            from .generated_questions import question_topics
            generated=question_topics(generated_questions,target['kind'])
            selected_pack={**selected_pack,
                           'topics':{**generated,**selected_pack['topics']},
                           'topic_order':[*generated,*selected_pack['topic_order']]}
            selected_pack['sha256']=digest({k:v for k,v in selected_pack.items() if k!='sha256'})
        if focus:
            from .focus import pack_with_focus
            selected_pack=pack_with_focus(selected_pack,focus)
        practice={}
        if value.include_practice_mistakes:
            from .practice_source import mistake_topics
            practice=await mistake_topics(conn,str(gid))
            if practice:
                # Practice topics go first: the user asked for these. They stay non-bank
                # topics with has_reference=False, so the gate can never score them.
                selected_pack={**selected_pack,
                               'topics':{**practice,**selected_pack['topics']},
                               'topic_order':[*practice,*selected_pack['topic_order']]}
        project_allowed=value.allow_project_questions if value.allow_project_questions is not None else not bool(value.question_banks)
        alignment=None
        if value.alignment_run_id:
            from interview_agent.materials.alignment_repository import current_alignment
            alignment=await current_alignment(conn,goal,material,target,value.alignment_run_id)
            if not {i for item in alignment['requirements'] for i in item['fact_ids']}<={f['id'] for f in facts}:
                raise ApiError('alignment_fact_scope_mismatch',400)
            preferred=[]
            for item in alignment['requirements']:
                for identity in item['topic_ids']:
                    for tid,data in selected_pack['topics'].items():
                        ref=data.get('bank_item',{})
                        if tid==identity or (ref.get('origin')=='builtin' and ref.get('item_id')==identity):preferred.append(tid)
            alignment['preferred_topic_ids']=list(dict.fromkeys(preferred))
        prior=await self._prior_observations(conn,gid,user_id,value.role,value.mode,selected_pack)
        payload={'policy':{'role':value.role,'mode':value.mode,'version':'ja05-policy-v1','max_questions':max_questions,'duration_seconds':duration_seconds},
                 'target':target,'facts':facts,'draft_id':draft_id,'application':application,'pack':selected_pack,
                 **({'focus':focus} if focus else {}),
                 'prior_observations':[{'id':str(r['id']),'source_session_id':str(r['session_id']),'answer_id':str(r['answer_id']),
                                       'answer_quote':r['payload']['answer_quote'],'pack_version':r['payload']['pack_version'],
                                       'topic_id':r['payload']['topic_id'],'label':r['payload']['label'],'summary':r['payload']['summary'],
                                       **({'summary_origin':r['payload']['summary_origin']} if 'summary_origin' in r['payload'] else {}),
                                       'assisted':r['payload']['assisted'],'goal_version':r['goal_version'],'application':r['application'],
                                       'origin':'prior_observation_to_retest'} for r in prior]}
        payload['training_context']={'jd_text':(goal.get('jd_text') or '')[:2000],'direction':((goal.get('conditions') or {}).get('direction') or '')[:120]}
        from .project_strategy import cards_for
        # F07Z-164: the user's own probing method comes first, then the preinstalled corpus.
        from interview_agent.strategies.repository import StrategyRepository
        user_cards=await StrategyRepository(self.pool).active_cards(conn,user_id,value.role)
        payload['strategies']=[*user_cards,*[card.as_dict() for card in cards_for(value.role)]]
        # F07Z-160: freeze the preinstalled skill body and digest with the session so a reopened
        # session replays the version it started with. Mode mismatch leaves the legacy rules.
        # F07Z-161: enabled user skills are frozen the same way (guidance only, never a tool grant).
        from interview_agent.skills.registry import as_dict as skill_dict
        from interview_agent.skills.registry import select as select_skill
        skill=select_skill('interview',value.mode)
        frozen_skills=[skill_dict(skill)] if skill is not None else []
        from interview_agent.skills.repository import SkillRepository
        frozen_skills.extend(await SkillRepository(self.pool).active_for_session(conn,user_id))
        if frozen_skills:payload['skills']=frozen_skills
        if handoff:payload['coordinator_handoff']=handoff
        from interview_agent.written.memory import active as written_memory
        payload['prior_written_results']=await written_memory(conn,gid,value.role)
        if alignment:payload['material_alignment']=alignment
        if wiki_id:payload['wiki_version_id']=wiki_id
        if jd_coverage:payload['jd_coverage']=jd_coverage
        payload['question_banks']=selected_banks
        if value.question_banks or value.allow_project_questions is not None:
            payload['policy']['allow_project_questions']=project_allowed
            payload['policy']['minimum_questions']=min(2,max_questions,len(selected_pack['topic_order'])+(1 if facts and project_allowed else 0))
        if value.profile=='full':
            # F07Z-167: a normal finish needs ten asked rounds when the candidates support them;
            # a thin pack lowers the bar instead of making the session unfinishable. The count
            # uses authored base/probe questions (retest is coaching-only) plus four project
            # focus dimensions per usable fact.
            rounds=capacity(selected_pack['topic_order'],selected_pack['topics'],facts,project_allowed)
            payload['policy']['profile']='full'
            payload['policy']['allow_project_questions']=project_allowed
            payload['policy']['minimum_questions']=minimum_questions('full',rounds)
            payload['policy']['full_simulation']={'version':'full-v1','target_low':10,'target_high':12,
                                                  'candidate_capacity':rounds}
        if value.mode=='interview' and facts and project_allowed and max_questions>=2 and selected_pack['topic_order'] and not practice:
            payload['policy']['coverage']={'version':'project-technical-v1'}
        if value.question_banks:
            payload['policy']['bank_source']={'mode':'bank_plus_role_pack','bank_items':bank_topics,
                                              'role_pack_items':len(selected_pack['topics'])-bank_topics}
        if practice:
            payload['policy']['include_practice_mistakes']=True
            payload['policy']['practice_mistake_count']=len(practice)
        if facts and project_allowed:
            payload['policy']['feedback_summary']={'version':PROJECT_SUMMARY_VERSION}
        s=await (await conn.execute('''INSERT INTO app.career_interviews(goal_id,goal_version,material_revision,memory_revision,request_id,request_hash,input_payload,deadline_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING *''',
            (gid,goal['version'],material['revision'],memory['revision'],key,hashed,Jsonb(payload),
             duration_seconds+(FULL_SETUP_GRACE_SECONDS if value.profile=='full' else 0)))).fetchone()
        run_id=await self._enqueue(conn,s)
        self.tracer.record('interview.created',stage='api',session_id=str(s['id']),goal_id=str(gid),
                       interview_run_id=run_id,question_count=0)
        return str(s['id']),True

    async def _prior_observations(self,conn,gid,user_id,role,mode,pack):
        """Cross-session evidence a new session may start from.

        S02b: a formal interview starts from nothing but its own session (S02A mode contract).
        Coaching may reuse this account/goal/role history; when the Store is available it
        supplies the topic-relevant candidate ids and SQL still re-validates every one of them
        (exists, completed, same role, not retracted). Any Store problem degrades to the same
        SQL query with the same filters and records why.
        """
        if mode!='coaching':return []
        where='''SELECT o.id,o.session_id,o.answer_id,o.payload,s.goal_version,s.input_payload->'application' AS application
            FROM app.career_interview_observations o
            JOIN app.career_interviews s ON s.id=o.session_id WHERE o.goal_id=%s AND s.status='COMPLETED'
            AND s.input_payload#>>'{policy,role}'=%s AND NOT EXISTS(SELECT 1 FROM app.career_interview_corrections c WHERE c.observation_id=o.id)'''
        tail=' ORDER BY o.created_at DESC,o.id DESC LIMIT 6'
        ids=None
        if self.projector is not None:
            try:
                ids=await self.projector.candidates(user_id,gid,role,topics=pack.get('topic_order'),limit=6)
            except Exception as error:
                self.tracer.record('memory.degraded',stage='memory',reason_code=type(error).__name__,
                                   goal_id=str(gid))
                ids=None
        if ids:
            rows=await (await conn.execute(where+' AND o.id=ANY(%s::uuid[])'+tail,(gid,role,ids))).fetchall()
            order={str(identity):index for index,identity in enumerate(ids)}
            return sorted(rows,key=lambda row:order.get(str(row['id']),len(order)))
        # An empty projection means the index has nothing yet (older sessions, fresh goal):
        # that is not "no history", so the same SQL query still answers. SQL stays authoritative.
        return await (await conn.execute(where+tail,(gid,role))).fetchall()

    async def _event(self,conn,sid,kind,payload,key=None,hashed=None):
        return await (await conn.execute('''INSERT INTO app.career_interview_events(session_id,seq,kind,payload,request_id,request_hash)
            SELECT %s,coalesce(max(seq),0)+1,%s,%s,%s,%s FROM app.career_interview_events WHERE session_id=%s RETURNING id''',
            (sid,kind,Jsonb(payload),key,hashed,sid))).fetchone()

    async def answer(self,principal,gid,sid,key,value):
        status='rejected'
        run_id=None
        try:
            async with self.pool.connection() as conn,conn.transaction():
                await require_session(conn,principal);root,s=await self._session(conn,gid,sid,principal.user_id)
                hashed=digest(value.model_dump(mode='json'))
                prior=await (await conn.execute('SELECT request_hash FROM app.career_interview_events WHERE session_id=%s AND request_id=%s',(sid,key))).fetchone()
                if prior:
                    if prior['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                    status='replayed'
                else:
                    if s['status']!='WAITING_FOR_USER' or s['version']!=value.expected_version:raise ApiError('version_conflict',409)
                    q=await (await conn.execute("SELECT id,payload FROM app.career_interview_events WHERE session_id=%s AND kind='question' ORDER BY seq DESC LIMIT 1",(sid,))).fetchone()
                    if str(q['id'])!=value.question_id:raise ApiError('question_changed',409)
                    await self._event(conn,sid,'answer',{'text':value.text,'question_id':str(q['id']),'topic_id':q['payload']['topic_id'],'assisted':q['payload']['assisted']},key,hashed)
                    s=await (await conn.execute("UPDATE app.career_interviews SET status='PENDING',version=version+1,revision=revision+1 WHERE id=%s RETURNING *",(sid,))).fetchone()
                    run_id=await self._enqueue(conn,s);status='accepted'
        except ApiError as error:
            # Rejected before any write committed; the trace only names the stable code.
            self.tracer.record('answer.rejected',stage='api',session_id=str(sid),goal_id=str(gid),
                               reason_code=error.code,error_category=observability.category_of(error.code))
            raise
        self.tracer.record('answer.'+status,stage='api',session_id=str(sid),goal_id=str(gid),
                           interview_run_id=run_id)

    async def skip(self,principal,gid,sid,key,value):
        """F07Z-167: record that the user passed on the current question, then ask for the next.

        It shares the answer's identity, session version, current question and idempotency key,
        so a racing answer/skip lets only the first commit win (the other sees a new version).
        """
        status='rejected';run_id=None
        try:
            async with self.pool.connection() as conn,conn.transaction():
                await require_session(conn,principal);root,s=await self._session(conn,gid,sid,principal.user_id)
                hashed=digest(value.model_dump(mode='json'))
                prior=await (await conn.execute('SELECT request_hash FROM app.career_interview_events WHERE session_id=%s AND request_id=%s',(sid,key))).fetchone()
                if prior:
                    if prior['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                    status='replayed'
                else:
                    if s['status']!='WAITING_FOR_USER' or s['version']!=value.expected_version:raise ApiError('version_conflict',409)
                    q=await (await conn.execute("SELECT id,payload FROM app.career_interview_events WHERE session_id=%s AND kind='question' ORDER BY seq DESC LIMIT 1",(sid,))).fetchone()
                    if str(q['id'])!=value.question_id:raise ApiError('question_changed',409)
                    await self._event(conn,sid,'skip',{'question_id':str(q['id']),'topic_id':q['payload']['topic_id']},key,hashed)
                    s=await (await conn.execute("UPDATE app.career_interviews SET status='PENDING',version=version+1,revision=revision+1 WHERE id=%s RETURNING *",(sid,))).fetchone()
                    run_id=await self._enqueue(conn,s);status='accepted'
        except ApiError as error:
            self.tracer.record('skip.rejected',stage='api',session_id=str(sid),goal_id=str(gid),
                               reason_code=error.code,error_category=observability.category_of(error.code))
            raise
        self.tracer.record('skip.'+status,stage='api',session_id=str(sid),goal_id=str(gid),
                           interview_run_id=run_id)

    async def clarify(self,principal,gid,sid,key,value):
        """F07Z-167: restate the frozen scope of the current question.

        It never changes the question, the round position or the clock, produces no observation
        and makes no model call. The text is assembled from the frozen question and topic title.
        """
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);root,s=await self._session(conn,gid,sid,principal.user_id)
            hashed=digest(value.model_dump(mode='json'))
            prior=await (await conn.execute('SELECT request_hash FROM app.career_interview_events WHERE session_id=%s AND request_id=%s',(sid,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                return
            if s['status']!='WAITING_FOR_USER' or s['version']!=value.expected_version:raise ApiError('version_conflict',409)
            question=await (await conn.execute("SELECT id,payload FROM app.career_interview_events WHERE session_id=%s AND kind='question' ORDER BY seq DESC LIMIT 1",(sid,))).fetchone()
            if str(question['id'])!=value.question_id:raise ApiError('question_changed',409)
            scope=await self._scope(conn,s)
            identity=question['payload']['topic_id']
            title=topic(scope,identity)['title'] if identity!='project' else '个人贡献深挖'
            text=(f'题意重述：{question["payload"].get("text","")}\n'
                  f'本题考查「{title}」。澄清只确认范围，不提供答案或提示，不改变题目，也不占题位。')
            await self._event(conn,sid,'clarification',
                              {'question_id':str(question['id']),'topic_id':identity,'text':text},key,hashed)

    async def stop(self,principal,gid,sid,value):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);root,s=await self._session(conn,gid,sid,principal.user_id)
            if s['version']!=value.expected_version:raise ApiError('version_conflict',409)
            if s['status'] in ACTIVE:await self._end(conn,s,'COMPLETED','user_stop')

    async def get(self,principal,gid,sid):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);root,s=await self._session(conn,gid,sid,principal.user_id)
            events=await (await conn.execute('SELECT id,seq,kind,payload FROM app.career_interview_events WHERE session_id=%s ORDER BY seq',(sid,))).fetchall()
            obs=await (await conn.execute('''SELECT o.id,o.answer_id,o.payload,c.reason AS correction FROM app.career_interview_observations o
                LEFT JOIN app.career_interview_corrections c ON c.observation_id=o.id WHERE o.session_id=%s ORDER BY o.created_at,o.id''',(sid,))).fetchall()
            run=await (await conn.execute('SELECT id,status,error_code,model_requests,result,origin FROM app.career_interview_runs WHERE session_id=%s ORDER BY session_version DESC LIMIT 1',(sid,))).fetchone()
            observations=[{'id':str(o['id']),'answer_id':str(o['answer_id']),**o['payload'],'correction':o['correction']} for o in obs]
            terminal=s['status'] not in ACTIVE
            visible=terminal or s['input_payload']['policy']['mode']=='coaching'
            questions=[e for e in events if e['kind']=='question'];answers=[e for e in events if e['kind']=='answer']
            report={'stop_reason':s['stop_reason'],'model_summary':s['model_summary'],'questions':len(questions),'answers':len(answers),
                    'assisted_answers':sum(bool(a['payload']['assisted']) for a in answers),'observations':observations,'semantic_scoring_eligible':False,
                    'unreviewed_answer_ids':[str(a['id']) for a in answers if str(a['id']) not in {o['answer_id'] for o in observations}],
                    'next_actions':[o['summary'] for o in observations if o['label']!='supported_in_this_answer' and not o['correction']],
                    # 新增业务 1: bank topics are frozen with a 'b-' prefix, so the report can say
                    # whether the interviewer actually used the user's bank in this session.
                    'bank_usage':{'bank_questions':sum(1 for q in questions
                                                       if str(q['payload'].get('topic_id') or '').startswith('b-')),
                                  'total_questions':len(questions),
                                  'declaration':_bank_declaration(run)},
                    # 收尾 01: the same observations, resolved to the authored retest question for
                    # that topic. Derived here so the client never guesses a question by keyword.
                    'retest':suggest_retest(observations,s['input_payload'].get('pack'))} if terminal else None
            if report is not None and any(item.get('generated') for item in s['input_payload']['pack']['topics'].values()):
                from .generated_questions import GENERATED_SOURCE, WIKI_SOURCE
                report['generated_questions']=sum(q['payload'].get('source')==GENERATED_SOURCE for q in questions)
                wiki_uses = [q['payload']['wiki'] for q in questions if q['payload'].get('source') == WIKI_SOURCE]
                if wiki_uses:
                    report['wiki_questions'] = len(wiki_uses)
                topics = s['input_payload']['pack']['topics']
                used_topics = dict.fromkeys(q['payload']['topic_id'] for q in questions
                                            if q['payload'].get('source') in (GENERATED_SOURCE, WIKI_SOURCE))
                references = [{'question_title': topics[tid]['title'], **topics[tid]['public_reference']}
                              for tid in used_topics if topics[tid].get('public_reference')]
                if references:
                    report['public_references'] = references
                experience = [{'question_title': topics[tid]['title'], **topics[tid]['experience_reference']}
                              for tid in used_topics if topics[tid].get('experience_reference')]
                if experience:
                    report['experience_references'] = experience
            if report is not None and s['model_summary'] is not None and project_summary_enabled(s['input_payload']):
                report['summary_origin']='server_session_counts'
            if report is not None and s['input_payload'].get('jd_coverage'):
                from .jd_coverage import project
                report['jd_coverage'] = project(s['input_payload']['jd_coverage'],
                                                (event['payload'] for event in questions))
            return {'id':str(s['id']),'goal_version':s['goal_version'],'material_revision':s['material_revision'],'version':s['version'],'revision':s['revision'],'status':s['status'],
                    'policy':s['input_payload']['policy'],'pack_version':s['input_payload']['pack']['version'],'target':s['input_payload']['target'],
                    'application':s['input_payload'].get('application'),
                    'material_fact_ids':[f['id'] for f in s['input_payload']['facts']],'draft_id':s['input_payload']['draft_id'],'deadline_at':s['deadline_at'],
                    **({'resume_evidence':[f for f in s['input_payload']['facts'] if f.get('resume_source')]} if any(f.get('resume_source') for f in s['input_payload']['facts']) else {}),
                    'material_alignment':s['input_payload'].get('material_alignment'),
                    'question_banks':s['input_payload'].get('question_banks',[]),
                    # F07Z-162: which skill versions this session used (no bodies; replay stays in
                    # the frozen payload). The review only needs the label and version.
                    'skills':[{k:item.get(k) for k in ('id','version','name','origin','author')}
                              for item in (s['input_payload'].get('skills')
                                           or ([s['input_payload']['skill']] if s['input_payload'].get('skill') else []))],
                    # F07Z-163: id -> title so the review can say which strategy card was used.
                    'strategies':[{k:item.get(k) for k in ('id','title')}
                                  for item in (s['input_payload'].get('strategies') or [])],
                    'events':[{**e,'id':str(e['id'])} for e in events],'observations':observations if visible else [],'report':report,
                    'run':{**{k:run[k] for k in ('status','error_code','model_requests','origin')},'id':str(run['id']),
                           'tool_log':(run['result'] or {}).get('tool_log',[]),
                           'diagnostic':run['result'] if run['status']=='FAILED' else None} if run else None}

    async def memory(self,principal,gid):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);await self._root(conn,gid,principal.user_id)
            rows=await (await conn.execute('''SELECT o.id,o.session_id,o.answer_id,o.payload,c.reason AS correction,s.goal_version FROM app.career_interview_observations o
                JOIN app.career_interviews s ON s.id=o.session_id LEFT JOIN app.career_interview_corrections c ON c.observation_id=o.id
                WHERE o.goal_id=%s AND (s.status NOT IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS') OR s.input_payload#>>'{policy,mode}'='coaching')
                ORDER BY o.created_at DESC,o.id DESC LIMIT 50''',(gid,))).fetchall()
            return {'items':[{**{k:r[k] for k in ('payload','correction','goal_version')},'id':str(r['id']),'session_id':str(r['session_id']),'answer_id':str(r['answer_id'])} for r in rows]}

    async def gap_report(self,principal,gid):
        """Aggregate what the interviews already recorded, per topic.

        Derived, not generated: every number comes from an existing immutable
        observation, so nothing here costs a model call. A topic counts as a gap when
        any attempt was `needs_probe`. `assisted` marks topics that were coached, so a
        coached topic is never presented as independent ability.
        """
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);await self._root(conn,gid,principal.user_id)
            rows=await (await conn.execute("""
                SELECT o.payload->>'topic_id' AS topic_id,
                       count(*) AS attempts,
                       count(DISTINCT o.session_id) AS sessions,
                       count(DISTINCT o.session_id) FILTER (WHERE o.payload->>'label'='needs_probe') AS probe_sessions,
                       count(DISTINCT o.session_id) FILTER (WHERE o.payload->>'label'='supported_in_this_answer') AS supported_sessions,
                       count(*) FILTER (WHERE o.payload->>'label'='needs_probe') AS needs_probe,
                       count(*) FILTER (WHERE o.payload->>'label'='supported_in_this_answer') AS supported,
                       count(*) FILTER (WHERE o.payload->>'label'='inconclusive') AS inconclusive,
                       bool_or(coalesce((o.payload->>'assisted')::boolean,false)) AS assisted,
                       max(o.created_at) AS last_at
                FROM app.career_interview_observations o
                WHERE o.goal_id=%s
                  AND NOT EXISTS(SELECT 1 FROM app.career_interview_corrections c WHERE c.observation_id=o.id)
                GROUP BY 1 ORDER BY needs_probe DESC,attempts DESC""",(gid,))).fetchall()
            latest=await (await conn.execute("""
                SELECT DISTINCT ON (o.payload->>'topic_id') o.payload->>'topic_id' AS topic_id,
                       o.payload->'summary' AS summary,o.payload->>'answer_quote' AS answer_quote,
                       o.payload->>'origin' AS origin,o.payload->>'label' AS label
                FROM app.career_interview_observations o
                WHERE o.goal_id=%s
                  AND NOT EXISTS(SELECT 1 FROM app.career_interview_corrections c WHERE c.observation_id=o.id)
                ORDER BY o.payload->>'topic_id',o.created_at DESC,o.id DESC""",(gid,))).fetchall()
        seen={r['topic_id']:r for r in latest}
        topics=[{'topic_id':r['topic_id'],'attempts':r['attempts'],'sessions':r['sessions'],
                 'needs_probe':r['needs_probe'],'supported':r['supported'],'inconclusive':r['inconclusive'],
                 'assisted':r['assisted'],
                 # Display gate (S02 §3.2): two different sessions in the same direction are a
                 # repeated observation; one session is not. Opposite directions coexist.
                 'repeated':r['probe_sessions']>=2 or r['supported_sessions']>=2,
                 'conflicting':r['probe_sessions']>0 and r['supported_sessions']>0,
                 'last_at':r['last_at'],'last_summary':(seen.get(r['topic_id']) or {}).get('summary') and seen[r['topic_id']]['summary'],
                 'last_answer_quote':(seen.get(r['topic_id']) or {}).get('answer_quote'),
                 'origin':(seen.get(r['topic_id']) or {}).get('origin')} for r in rows]
        attempts=sum(t['attempts'] for t in topics)
        probe=sum(t['needs_probe'] for t in topics)
        supported=sum(t['supported'] for t in topics)
        # Without a real model every observation is 'inconclusive', so the loop has no
        # signal to act on. Say that out loud instead of presenting zero gaps as success.
        signal=('no_observations' if attempts==0 else 'no_signal' if probe==0 and supported==0
                else 'conflicting' if any(t['conflicting'] for t in topics)
                else 'repeated' if any(t['repeated'] for t in topics) else 'partial')
        return {'goal_version':(await (await conn.execute('SELECT version FROM app.career_goals WHERE id=%s',(gid,))).fetchone())['version'],
                'topics':topics,
                'summary':{'topics':len(topics),'attempts':attempts,'needs_probe_topics':sum(1 for t in topics if t['needs_probe']),
                           'assisted_topics':sum(1 for t in topics if t['assisted']),
                           'repeated_topics':sum(1 for t in topics if t['repeated']),
                           'conflicting_topics':sum(1 for t in topics if t['conflicting']),'signal':signal},
                'note':('尚无作答记录' if signal=='no_observations'
                        else '当前观察全部是脚本判定，无法区分"答得好"与"答得差"，这份报告只能当记录看' if signal=='no_signal'
                        else '存在可用的判定：needs_probe 的主题就是待补的缺口' if signal=='partial'
                        else '不同场次出现同向证据：这是重复观察，不是能力评分' if signal=='repeated'
                        else '不同场次出现相反证据：两边都保留，建议独立复测' if signal=='conflicting'
                        else '存在可用的判定')}

    async def listing(self,principal,gid):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);await self._root(conn,gid,principal.user_id)
            rows=await (await conn.execute('SELECT id FROM app.career_interviews WHERE goal_id=%s ORDER BY created_at DESC LIMIT 20',(gid,))).fetchall()
        return [await self.get(principal,gid,str(r['id'])) for r in rows]

    async def correct(self,principal,gid,oid,key,value):
        async with self.pool.connection() as conn,conn.transaction():
            await require_session(conn,principal);await self._root(conn,gid,principal.user_id);hashed=digest([oid,value.model_dump()])
            prior=await (await conn.execute('SELECT request_hash FROM app.career_interview_corrections WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
            if prior:
                if prior['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                return
            row=await (await conn.execute('''SELECT o.id FROM app.career_interview_observations o JOIN app.career_interviews s ON s.id=o.session_id
                WHERE o.id=%s AND o.goal_id=%s AND (s.status NOT IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS') OR s.input_payload#>>'{policy,mode}'='coaching')''',(oid,gid))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            if await (await conn.execute('SELECT id FROM app.career_interview_corrections WHERE observation_id=%s',(oid,))).fetchone():raise ApiError('observation_already_retracted',409)
            await conn.execute('INSERT INTO app.career_interview_corrections(goal_id,observation_id,request_id,request_hash,reason) VALUES(%s,%s,%s,%s,%s)',(gid,oid,key,hashed,value.reason))
            await conn.execute('UPDATE app.career_interview_memory_state SET revision=revision+1 WHERE goal_id=%s',(gid,))

    async def _lock(self,conn,message):
        ref=await (await conn.execute('SELECT goal_id,session_id FROM app.career_interview_runs WHERE id=%s AND job_id=%s',(message.operation_id,message.job_id))).fetchone()
        if not ref:return None
        root,s=await self._session(conn,ref['goal_id'],ref['session_id'])
        run=await (await conn.execute('SELECT * FROM app.career_interview_runs WHERE id=%s FOR UPDATE',(message.operation_id,))).fetchone()
        return root,s,run

    async def _owned(self,conn,claim):
        rows=await self._lock(conn,claim.message)
        if not rows:return None
        root,s,r=rows;now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (s['status']!='RUNNING' or r['status']!='RUNNING' or r['attempt']!=claim.attempt or r['fencing_token']!=claim.fencing_token or r['lease_until']<=now
            or s['version']!=r['session_version'] or str(root[0]['user_id'])!=claim.user_id or str(root[0]['id'])!=claim.resource_id or r['goal_version']!=claim.message.resource_version):return None
        return rows

    async def pending_dispatch(self,recovery_seconds=5):
        async with self.pool.connection() as conn:
            sessions=await (await conn.execute("SELECT id,goal_id FROM app.career_interviews WHERE status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS') ORDER BY created_at,id LIMIT 100")).fetchall()
            for item in sessions:
                # A session whose goal was removed (test cleanup, deleted goal) must not abort the
                # whole dispatch loop: that exception escaped once() and killed the worker, and the
                # same stale row did it again on every restart.
                try:
                    async with conn.transaction():await self._session(conn,item['goal_id'],item['id'])
                except ApiError:
                    self.tracer.record('worker.stale_outbox',stage='queue',
                                       reason_code='resource_unavailable',error_category='queue',
                                       session_id=str(item['id']))
                    continue
            return await (await conn.execute("""SELECT r.id AS outbox_id,r.id AS operation_id,r.job_id,r.goal_version AS resource_version
                FROM app.career_interview_runs r JOIN app.career_interviews s ON s.id=r.session_id
                WHERE s.status IN ('PENDING','RUNNING') AND (r.status='PENDING' OR (r.status='RUNNING' AND r.lease_until<=clock_timestamp()))
                AND (r.dispatched_at IS NULL OR r.dispatched_at<=clock_timestamp()-(%s*interval '1 second')) ORDER BY r.created_at LIMIT 100""",(recovery_seconds,))).fetchall()

    async def mark_dispatched(self,identity):
        async with self.pool.connection() as conn:await conn.execute('UPDATE app.career_interview_runs SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s',(identity,))

    def _claim_blocked(self,message,code,session_id=None):
        """Record why a claim was refused. Metadata only; the decision already happened."""
        self.tracer.record('worker.claim_blocked',stage='worker',reason_code=code,
                           error_category=observability.category_of(code),job_id=message.job_id,
                           operation_id=message.operation_id,session_id=session_id)

    async def claim(self,message,lease_seconds=30):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            if not rows:
                self._claim_blocked(message,'run_unavailable');return None
            root,s,r=rows;now=(await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if s['status'] not in ('PENDING','RUNNING') or r['status'] not in ('PENDING','RUNNING') or r['goal_version']!=message.resource_version or r['session_version']!=s['version']:
                self._claim_blocked(message,'session_not_runnable',str(s['id']));return None
            if r['status']=='RUNNING' and r['lease_until']>now:
                self._claim_blocked(message,'lease_held',str(s['id']));return None
            if r['attempt']>=3:
                await self._end(conn,s,'FAILED','attempt_limit');self._claim_blocked(message,'attempt_limit',str(s['id']));return None
            row=await (await conn.execute("UPDATE app.career_interview_runs SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,lease_until=clock_timestamp()+(%s*interval '1 second'),revision=revision+1 WHERE id=%s RETURNING *",(duration(lease_seconds),r['id']))).fetchone()
            await conn.execute("UPDATE app.career_interviews SET status='RUNNING',revision=revision+1 WHERE id=%s",(s['id'],))
            return Claim(message,str(root[0]['user_id']),str(root[0]['id']),row['attempt'],row['fencing_token'],'',kind='interview_agent',session_id=str(s['id']),session_version=s['version'])

    async def heartbeat(self,claim,lease_seconds=30):
        async with self.pool.connection() as conn,conn.transaction():
            if not await self._owned(conn,claim):return False
            await conn.execute("UPDATE app.career_interview_runs SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",(duration(lease_seconds),claim.message.operation_id));return True

    async def context(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise ValueError('interview_claim_expired')
            return rows[2]['input_payload']

    async def cached(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise ValueError('interview_claim_expired')
            return rows[2]['candidate']

    async def admit_scripted(self,claim):
        from interview_agent.billing.ledger import BillingUnavailable
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:raise BillingUnavailable('model_execution_expired')
            root,s,r=rows
            if legacy_budget_exhausted(root[0]) or root[0]['training_model_requests']>=80 or s['model_requests']>=80 or r['model_requests']>=5:raise BillingUnavailable('model_budget_exhausted')
            await self._increment(conn,root[0],s,r)

    async def _increment(self,conn,goal,s,r):
        await conn.execute('UPDATE app.career_interview_runs SET model_requests=model_requests+1 WHERE id=%s',(r['id'],))
        await conn.execute('UPDATE app.career_interviews SET model_requests=model_requests+1,revision=revision+1 WHERE id=%s',(s['id'],))
        await conn.execute('UPDATE app.career_goals SET model_requests=model_requests+1,training_model_requests=training_model_requests+1,revision=revision+1 WHERE id=%s',(goal['id'],))

    async def save(self,claim,value,origin):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            d=validate_decision(rows[2]['input_payload'],value)
            await conn.execute('UPDATE app.career_interview_runs SET candidate=%s,origin=%s WHERE id=%s',(Jsonb(d.model_dump(mode='json')),origin,claim.message.operation_id));return True

    async def complete(self,claim):
        committed=await self._complete(claim)
        if committed and self.projector is not None and claim.session_id:
            # After commit: a projection failure can never roll back or fail the interview.
            await self.projector.project_session(self.pool,claim.session_id)
        return committed

    async def _complete(self,claim):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            root,s,r=rows;scope=r['input_payload'];d=validate_decision(scope,r['candidate'])
            if d.observation:
                ref=next(x for x in topic(scope,scope['current_question']['topic_id'])['references'] if x['id']==d.observation.evidence_id)
                payload={**d.observation.model_dump(),'topic_id':scope['current_question']['topic_id'],'assisted':scope['answer']['assisted'],'origin':r['origin'],
                         'evidence_source':{**{k:ref.get(k) for k in ('id','url','origin','version')},
                                            **({'resume_source':ref['resume_source']} if ref.get('resume_source') else {})},
                         'pack_version':scope['pack']['version'],'verification':'model_observation_not_ability_certification'}
                if scope['current_question']['topic_id']=='project' and project_summary_enabled(scope):
                    payload['summary_origin']='server_answer_excerpt'
                await conn.execute('INSERT INTO app.career_interview_observations(goal_id,session_id,answer_id,payload) VALUES(%s,%s,%s,%s)',(s['goal_id'],s['id'],scope['answer']['id'],Jsonb(payload)))
            if d.action=='finish':
                await conn.execute("UPDATE app.career_interviews SET status='COMPLETED',stop_reason=%s,model_summary=%s,revision=revision+1 WHERE id=%s",('question_limit' if s['question_count']>=scope['policy']['max_questions'] else 'agent_finished',d.summary,s['id']))
            else:
                if d.action=='teach_and_retest':await self._event(conn,s['id'],'explanation',{'topic_id':d.topic_id,'text':d.explanation,'evidence_id':d.observation.evidence_id,'evidence_quote':d.observation.evidence_quote,'origin':r['origin']})
                question = question_payload(scope,d)
                event = await self._event(conn,s['id'],'question',question)
                if question.get('source') == 'jd_generated' and question['kind'] == 'base':
                    from interview_agent.interview_wiki.repository import record_used_question
                    await record_used_question(conn,root[0]['user_id'],event['id'],scope['pack']['topics'][d.topic_id])
                await conn.execute("UPDATE app.career_interviews SET status='WAITING_FOR_USER',question_count=question_count+1,revision=revision+1 WHERE id=%s",(s['id'],))
                if s['question_count']==0 and scope['policy'].get('profile')=='full':
                    # F07Z-167: the full simulation's clock starts when the first question is
                    # published; preparation and the first decision run on the setup allowance.
                    await conn.execute("UPDATE app.career_interviews SET deadline_at=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",(scope['policy']['duration_seconds'],s['id']))
            await conn.execute("UPDATE app.career_interview_runs SET status='SUCCEEDED',lease_until=NULL,result=%s,revision=revision+1 WHERE id=%s",(Jsonb(d.model_dump(mode='json')),r['id']))
            self.tracer.record('interview.decision',stage='worker',session_id=str(s['id']),interview_run_id=str(r['id']),
                               action=d.action,label=(d.observation.label if d.observation else None),
                               question_count=len(scope['question_history']),provider=r['origin'],
                               topic_id=d.topic_id or (scope.get('current_question') or {}).get('topic_id'),
                               evidence_ids=[d.observation.evidence_id] if d.observation else None)
            if d.action=='finish':
                from interview_agent.career.handoffs import continue_after
                await continue_after(conn,root[0],s['input_payload'],'interview',s['id'],'COMPLETED')
            return True

    async def fail_assessment(self,claim,**kwargs):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._owned(conn,claim)
            if not rows:return False
            status='WAITING_CREDENTIALS' if kwargs.get('waiting') else 'FAILED'
            code=kwargs.get('code','interview_failed')
            await conn.execute('UPDATE app.career_interview_runs SET status=%s,lease_until=NULL,error_code=%s,result=%s,revision=revision+1 WHERE id=%s',
                               (status,code,Jsonb(kwargs['diagnostic']) if kwargs.get('diagnostic') else None,claim.message.operation_id))
            await conn.execute('UPDATE app.career_interviews SET status=%s,revision=revision+1 WHERE id=%s',(status,rows[1]['id']))
            if status=='WAITING_CREDENTIALS':
                from interview_agent.career.handoffs import pause_tool_parent
                await pause_tool_parent(conn,rows[0][0]['id'],rows[1]['input_payload'],'admission_wait','interview')
            if status=='FAILED':
                from interview_agent.career.handoffs import continue_after
                resumed=await continue_after(conn,rows[0][0],rows[1]['input_payload'],'interview',rows[1]['id'],'FAILED')
                if not resumed:
                    from interview_agent.career.handoffs import pause_tool_parent
                    await pause_tool_parent(conn,rows[0][0]['id'],rows[1]['input_payload'],'task_failed','interview')
            self.tracer.record('interview.failed',stage='worker',session_id=str(rows[1]['id']),
                               interview_run_id=str(claim.message.operation_id),reason_code=code,
                               error_category=observability.category_of(code),outcome=status)
            return True

    async def fail_attempt(self,claim):return await self.fail_assessment(claim)

    async def can_ack(self,message):
        async with self.pool.connection() as conn,conn.transaction():
            rows=await self._lock(conn,message)
            return not rows or rows[1]['status'] not in ('PENDING','RUNNING') or rows[2]['status'] in RUN_TERMINAL or rows[1]['version']!=rows[2]['session_version']
