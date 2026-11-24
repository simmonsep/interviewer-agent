"""Interview preparation: SQL-authoritative state, one row per preparation.

The row is both the business record and the attempt record (attempt/lease/fence) because a
preparation is short-lived: unlike a whole interview there is no second *runs table. Redis is
only a wake-up; every state transition here is fenced and idempotent.
"""
from psycopg.types.json import Jsonb

from interview_agent import observability
from interview_agent.career.repository import digest
from interview_agent.interviews.simulation import resolved
from interview_agent.materials.repository import MaterialRepository
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim, duration

ACTIVE = ('PENDING', 'RUNNING', 'WAITING_FOR_USER')
PREPARATION_LIMIT = 20


class PreparationNotConfirmed(Exception):
    """The graph tried to open a session without an approved, current decision."""


def canonical(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def plan_sha256(plan):
    import hashlib
    return hashlib.sha256(canonical(plan).encode()).hexdigest()


def view(row):
    plan = row['plan']
    if plan and 'generated_questions' in plan:
        plan = dict(plan)
        if plan.pop('generated_questions'):
            plan['rationale'] = ('使用已审核的面试 Wiki 题目。' if plan.get('wiki_question_count')
                                 else '根据岗位要求准备补充题目。')
    if plan and 'jd_coverage' in plan:
        from interview_agent.interviews.jd_coverage import project
        plan = {**plan, 'jd_coverage': project(plan['jd_coverage'])}
        if plan.get('rationale') and not plan.get('generated_question_count') and not plan.get('wiki_question_count'):
            plan['rationale'] = '根据岗位要求选择已有题目。'
    return {
        'id': str(row['id']), 'status': row['status'], 'goal_version': row['goal_version'],
        'version': row['version'], 'role': row['role'], 'mode': row['mode'],
        'profile': row.get('profile'),
        'max_questions': row['max_questions'], 'duration_seconds': row['duration_seconds'],
        'draft_id': str(row['draft_id']) if row['draft_id'] else None,
        'use_resume': row['use_resume'],
        'plan': plan, 'plan_sha256': row['plan_sha256'], 'decision': row['decision'],
        'session_id': str(row['session_id']) if row['session_id'] else None,
        'frozen_memory': [{'id': item.get('id'), 'topic_id': item.get('topic_id'),
                           'label': item.get('label'), 'summary': str(item.get('summary') or '')[:200]}
                          for item in ((row['frozen'] or {}).get('memory') or [])][:24],
        'error_code': row['error_code'], 'attempt': row['attempt'],
        'created_at': row['created_at'].isoformat(),
    }


class PreparationRepository:
    def __init__(self, pool, tracer=None):
        self.pool = pool
        self.tracer = tracer if tracer is not None else observability.current()

    # ------------------------------------------------------------------ API surface

    async def create(self, principal, gid, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            pid, created = await self.create_at(conn, principal.user_id, gid, key, value)
        if created:
            self.tracer.record('preparation.created', stage='preparation', session_id=None,
                               outcome='created', goal_id=str(gid))
        return pid, created

    async def create_at(self, conn, user_id, gid, key, value):
        """Create a preparation inside a caller-owned transaction.

        F07Z-166: the career coordinator calls this while it already holds the goal row lock, so
        the proposal and the preparation commit together (no window where one exists without the
        other). Ownership/version checks are the same as the API path; only the transaction owner
        differs.
        """
        goal = await (await conn.execute(
            'SELECT * FROM app.career_goals WHERE id=%s AND user_id=%s FOR UPDATE',
            (gid, user_id))).fetchone()
        if not goal:
            raise ApiError('resource_unavailable', 404)
        # F07Z-152: default to the selected position's bound resume when none is given.
        # F07Z-166: an explicit "no resume" wins over that default.
        if value.use_resume and value.draft_id is None:
            bound=await (await conn.execute('''SELECT b.resume_draft_id FROM app.career_job_selections sel
                JOIN app.career_job_snapshots s ON s.id=sel.snapshot_id
                JOIN app.career_position_bindings b ON b.position_id=s.position_id AND b.active
                WHERE sel.goal_id=%s AND sel.goal_version=%s''',(gid,goal['version']))).fetchone()
            if bound:value=value.model_copy(update={'draft_id':str(bound['resume_draft_id'])})
        body = value.model_dump(mode='json')
        # Keep the hash of requests created before this field existed.
        if value.use_resume:
            body.pop('use_resume', None)
        if value.draft_id is None:
            body.pop('draft_id')
        if not value.resume_item_indexes:
            body.pop('resume_item_indexes')
        if value.allow_project_questions is None:
            body.pop('allow_project_questions')
        if not value.question_banks:
            body.pop('question_banks')
        hashed = digest(body)
        prior = await (await conn.execute(
            'SELECT id,request_hash FROM app.career_interview_preparations WHERE goal_id=%s AND request_id=%s',
            (gid, key))).fetchone()
        if prior:
            if prior['request_hash'] != hashed:
                raise ApiError('idempotency_conflict', 409)
            return str(prior['id']), False
        if goal['version'] != value.expected_version or goal['status'] == 'CANCELLED':
            raise ApiError('version_conflict', 409)
        if value.role is None:
            value = value.model_copy(update={'role': await self._resolved_role(conn, goal)})
        active = await (await conn.execute(
            "SELECT id FROM app.career_interview_preparations WHERE goal_id=%s AND status IN "
            "('PENDING','RUNNING','WAITING_FOR_USER') FOR UPDATE", (gid,))).fetchone()
        if active:
            raise ApiError('preparation_in_progress', 409)
        count = await (await conn.execute(
            'SELECT count(*) AS n FROM app.career_interview_preparations WHERE goal_id=%s', (gid,))).fetchone()
        if count['n'] >= PREPARATION_LIMIT:
            raise ApiError('preparation_limit', 409)
        material = await MaterialRepository(self.pool)._state(conn, gid)
        if value.draft_id:
            draft = await (await conn.execute(
                'SELECT * FROM app.career_material_drafts WHERE id=%s AND goal_id=%s',
                (value.draft_id, gid))).fetchone()
            if not draft:
                raise ApiError('resource_unavailable', 404)
            target = await MaterialRepository(self.pool)._target(conn, goal)
            if (draft['goal_version'] != goal['version'] or draft['material_revision'] != material['revision']
                    or digest(draft['payload']['target']) != digest(target)):
                raise ApiError('draft_stale', 409)
        selected = {}
        if value.question_banks:
            from interview_agent.question_banks.repository import BankRepository
            await BankRepository(self.pool).resolve(conn, user_id, value.role, value.question_banks)
            selected['question_banks'] = body['question_banks']
        if value.allow_project_questions is not None:
            selected['allow_project_questions'] = value.allow_project_questions
        row = await (await conn.execute(
            '''INSERT INTO app.career_interview_preparations(goal_id,user_id,goal_version,material_revision,
               role,mode,profile,max_questions,duration_seconds,draft_id,use_resume,resume_item_indexes,request_id,request_hash,frozen)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (gid, user_id, goal['version'], material['revision'], value.role, value.mode,
             value.profile, *resolved(value.profile, value.max_questions, value.duration_seconds),
             value.draft_id, value.use_resume,
             Jsonb(value.resume_item_indexes), key, hashed, Jsonb(selected) if selected else None))).fetchone()
        return str(row['id']), True

    async def get(self, principal, gid, pid):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            row = await self._locked(conn, gid, pid, principal.user_id)
            row = await self._refresh(conn, row)
            return view(row)

    async def current(self, principal, gid):
        """The goal's latest preparation, so a reload can resume it without a local id."""
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            row = await (await conn.execute(
                '''SELECT * FROM app.career_interview_preparations
                    WHERE goal_id=%s AND user_id=%s
                    ORDER BY created_at DESC,id DESC LIMIT 1''',
                (gid, principal.user_id))).fetchone()
            if not row:
                raise ApiError('resource_unavailable', 404)
            row = await self._refresh(conn, row)
            return view(row)

    async def confirm(self, principal, gid, pid, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            row = await self._locked(conn, gid, pid, principal.user_id)
            row = await self._refresh(conn, row)
            hashed = digest([pid, value.model_dump(mode='json')])
            decision = row['decision'] or {}
            if decision:
                if decision.get('request_id') == key:
                    if decision.get('request_hash') != hashed:
                        raise ApiError('idempotency_conflict', 409)
                    return view(row)
                if decision.get('decision') != value.decision:
                    raise ApiError('preparation_already_decided', 409)
                return view(row)
            if row['status'] != 'WAITING_FOR_USER':
                raise ApiError('preparation_not_awaiting_confirmation', 409)
            if row['version'] != value.expected_version:
                raise ApiError('version_conflict', 409)
            if value.plan_sha256 != row['plan_sha256']:
                raise ApiError('preparation_plan_changed', 409)
            status = 'PENDING' if value.decision == 'approve' else 'REJECTED'
            record = {'decision': value.decision, 'reason': value.reason, 'request_id': key,
                      'request_hash': hashed, 'plan_sha256': value.plan_sha256,
                      'decided_by': 'authenticated_user'}
            row = await (await conn.execute(
                '''UPDATE app.career_interview_preparations SET status=%s,decision=%s,version=version+1,
                   revision=revision+1,attempt=0,dispatched_at=NULL,lease_until=NULL,
                   fencing_token=fencing_token+1,updated_at=clock_timestamp() WHERE id=%s RETURNING *''',
                (status, Jsonb(record), pid))).fetchone()
        self.tracer.record('preparation.confirmed', stage='preparation', outcome=value.decision,
                           session_id=str(pid), goal_id=str(gid))
        return view(row)

    # ------------------------------------------------------------------ dispatch / worker

    async def pending_dispatch(self, recovery_seconds=5):
        async with self.pool.connection() as conn:
            return await (await conn.execute('''SELECT id AS outbox_id,id AS operation_id,id AS job_id,
                version AS resource_version FROM app.career_interview_preparations
                WHERE status IN ('PENDING','RUNNING')
                AND (status='PENDING' OR lease_until<=clock_timestamp())
                AND (dispatched_at IS NULL OR dispatched_at<=clock_timestamp()-(%s*interval '1 second'))
                ORDER BY created_at,id LIMIT 100''', (recovery_seconds,))).fetchall()

    async def mark_dispatched(self, identity):
        async with self.pool.connection() as conn:
            await conn.execute('UPDATE app.career_interview_preparations SET dispatched_at=clock_timestamp(),'
                               'dispatch_count=dispatch_count+1 WHERE id=%s', (identity,))

    def _blocked(self, message, code, prep_id=None):
        self.tracer.record('preparation.claim_blocked', stage='preparation', reason_code=code,
                           error_category=observability.category_of(code),
                           session_id=str(prep_id) if prep_id else None)

    async def claim(self, message, lease_seconds=30):
        duration(lease_seconds)
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                'SELECT * FROM app.career_interview_preparations WHERE id=%s AND id=%s FOR UPDATE',
                (message.operation_id, message.job_id))).fetchone()
            if not row:
                self._blocked(message, 'run_unavailable')
                return None
            if row['version'] != message.resource_version or row['status'] not in ('PENDING', 'RUNNING'):
                self._blocked(message, 'session_not_runnable', row['id'])
                return None
            now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if row['status'] == 'RUNNING' and row['lease_until'] and row['lease_until'] > now:
                self._blocked(message, 'lease_held', row['id'])
                return None
            if row['attempt'] >= 3:
                await self._finish(conn, row['id'], 'FAILED', 'attempt_limit')
                self._blocked(message, 'attempt_limit', row['id'])
                return None
            row = await (await conn.execute(
                '''UPDATE app.career_interview_preparations SET status='RUNNING',attempt=attempt+1,
                   fencing_token=fencing_token+1,lease_until=clock_timestamp()+(%s*interval '1 second'),
                   revision=revision+1,updated_at=clock_timestamp() WHERE id=%s
                   RETURNING attempt,fencing_token,user_id,goal_id''', (duration(lease_seconds), row['id']))).fetchone()
            return Claim(message, str(row['user_id']), str(row['goal_id']), row['attempt'],
                         row['fencing_token'], '', kind='interview_preparation')

    async def _owned(self, conn, claim):
        row = await (await conn.execute(
            'SELECT * FROM app.career_interview_preparations WHERE id=%s FOR UPDATE',
            (claim.message.operation_id,))).fetchone()
        if not row:
            return None
        now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (row['status'] != 'RUNNING' or row['attempt'] != claim.attempt
                or row['fencing_token'] != claim.fencing_token or row['version'] != claim.message.resource_version
                or not row['lease_until'] or row['lease_until'] <= now
                or str(row['user_id']) != claim.user_id or str(row['goal_id']) != claim.resource_id):
            return None
        return row

    async def heartbeat(self, claim, lease_seconds=30):
        duration(lease_seconds)
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim):
                return False
            await conn.execute('UPDATE app.career_interview_preparations SET lease_until='
                               'clock_timestamp()+(%s*interval \'1 second\'),updated_at=clock_timestamp() '
                               'WHERE id=%s', (duration(lease_seconds), claim.message.operation_id))
            return True

    async def context(self, claim):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self._owned(conn, claim)
            if not row:
                raise ValueError('preparation_claim_expired')
            return row

    async def approved_decision(self, claim):
        """Server-side truth for `start_interview`: an approved decision for the current plan."""
        row = await self.context(claim)
        decision = row['decision'] or {}
        if (decision.get('decision') != 'approve' or not row['plan_sha256']
                or decision.get('plan_sha256') != row['plan_sha256']):
            raise PreparationNotConfirmed('preparation_not_confirmed')
        return decision

    async def freeze(self, claim, frozen):
        """Persist what `load_inputs` froze. Idempotent: a retry writes the same snapshot."""
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim):
                return False
            await conn.execute('UPDATE app.career_interview_preparations SET frozen=%s,revision=revision+1,'
                               'updated_at=clock_timestamp() WHERE id=%s', (Jsonb(frozen), claim.message.operation_id))
            return True

    async def admit_experience_search(self, claim):
        """Reserve the one search before sending it, including across lease recovery."""
        async with self.pool.connection() as conn, conn.transaction():
            row = await self._owned(conn, claim)
            frozen = (row or {}).get('frozen') or {}
            if not frozen.get('question_basis') or row['experience_search_admitted_at'] is not None:
                return False
            await conn.execute(
                'UPDATE app.career_interview_preparations SET experience_search_admitted_at=clock_timestamp(),'
                'revision=revision+1 WHERE id=%s',
                (claim.message.operation_id,))
            return True

    async def mark_waiting(self, claim, plan):
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim):
                return False
            row = await (await conn.execute(
                '''UPDATE app.career_interview_preparations SET status='WAITING_FOR_USER',plan=%s,
                   plan_sha256=%s,lease_until=NULL,revision=revision+1,updated_at=clock_timestamp()
                   WHERE id=%s RETURNING id''', (Jsonb(plan), plan_sha256(plan), claim.message.operation_id))).fetchone()
        self.tracer.record('preparation.waiting', stage='preparation', session_id=str(row['id']))
        return True

    async def start_session(self, claim, session_id):
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim):
                return False
            await conn.execute('''UPDATE app.career_interview_preparations SET status='SUCCEEDED',session_id=%s,
                lease_until=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s''',
                               (session_id, claim.message.operation_id))
        self.tracer.record('preparation.started', stage='preparation', session_id=str(claim.message.operation_id))
        return True

    async def fail(self, claim, code, *, status='FAILED', detail=None):
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim):
                return False
            await self._finish(conn, claim.message.operation_id, status, code, detail)
        self.tracer.record('preparation.failed', stage='preparation', reason_code=code,
                           error_category=observability.category_of(code), outcome=status)
        return True

    async def complete(self, claim):
        """The attempt ended without an exception: release the lease, keep the business status."""
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                'SELECT attempt,fencing_token,version FROM app.career_interview_preparations WHERE id=%s FOR UPDATE',
                (claim.message.operation_id,))).fetchone()
            if (not row or row['attempt'] != claim.attempt or row['fencing_token'] != claim.fencing_token
                    or row['version'] != claim.message.resource_version):
                return False
            await conn.execute('UPDATE app.career_interview_preparations SET lease_until=NULL,'
                               'updated_at=clock_timestamp() WHERE id=%s', (claim.message.operation_id,))
            return True

    async def fail_attempt(self, claim):
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim):
                return False
            if claim.attempt >= 3:
                await self._finish(conn, claim.message.operation_id, 'FAILED', 'worker_execution_failed')
            else:
                await conn.execute("UPDATE app.career_interview_preparations SET status='PENDING',"
                                   "lease_until=NULL,dispatched_at=NULL,error_code='worker_execution_failed',"
                                   "revision=revision+1,updated_at=clock_timestamp() WHERE id=%s",
                                   (claim.message.operation_id,))
            return True

    async def can_ack(self, message):
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                'SELECT status,version FROM app.career_interview_preparations WHERE id=%s',
                (message.operation_id,))).fetchone()
            return (row is None or row['version'] != message.resource_version
                    or row['status'] not in ('PENDING', 'RUNNING'))

    # ------------------------------------------------------------------ internals

    async def _resolved_role(self, conn, goal):
        """F07Z-166: one deterministic resolver, shared with the auto-prepared path."""
        from interview_agent.career.roles import resolve_role
        selected = await (await conn.execute(
            '''SELECT s.payload->>'title' AS title FROM app.career_job_selections sel
               JOIN app.career_job_snapshots s ON s.id=sel.snapshot_id
               WHERE sel.goal_id=%s AND sel.goal_version=%s''',
            (goal['id'], goal['version']))).fetchone()
        role = resolve_role(direction=(goal['conditions'] or {}).get('direction') or '',
                            title=(selected or {}).get('title') or '', message='')
        if role is None:
            raise ApiError('role_required', 409)
        return role

    async def _locked(self, conn, gid, pid, user_id):
        row = await (await conn.execute(
            'SELECT * FROM app.career_interview_preparations WHERE id=%s AND goal_id=%s AND user_id=%s FOR UPDATE',
            (pid, gid, user_id))).fetchone()
        if not row:
            raise ApiError('resource_unavailable', 404)
        return row

    async def _refresh(self, conn, row):
        """Mark a preparation stale when its frozen context no longer matches the goal."""
        if row['status'] not in ACTIVE:
            return row
        goal = await (await conn.execute(
            'SELECT version,status FROM app.career_goals WHERE id=%s', (row['goal_id'],))).fetchone()
        material = await MaterialRepository(self.pool)._state(conn, row['goal_id'])
        if (not goal or goal['status'] == 'CANCELLED' or goal['version'] != row['goal_version']
                or material['revision'] != row['material_revision']):
            return await self._finish(conn, row['id'], 'STALE', 'context_changed')
        wiki = (row['frozen'] or {}).get('wiki_question')
        if wiki:
            from interview_agent.interview_wiki.repository import usable
            if not await usable(conn, row['user_id'], wiki['wiki']['id']):
                return await self._finish(conn, row['id'], 'STALE', 'wiki_source_withdrawn')
        return row

    async def _finish(self, conn, pid, status, code=None, detail=None):
        return await (await conn.execute(
            '''UPDATE app.career_interview_preparations SET status=%s,error_code=%s,error_detail=%s,
               lease_until=NULL,fencing_token=fencing_token+1,revision=revision+1,updated_at=clock_timestamp()
               WHERE id=%s RETURNING *''',
            (status, code, Jsonb(detail) if detail is not None else None, pid))).fetchone()

    async def frozen_inputs(self, row):
        """Read the goal and material state, including the private planner's question basis."""
        from interview_agent.materials.repository import MaterialRepository as Materials
        from interview_agent.career.repository import digest
        from interview_agent.connectors.public_reference import explicit_topic
        from interview_agent.interview_wiki.repository import active_question
        from interview_agent.interviews.jd_coverage import include_generated, requirements
        from interview_agent.interviews.catalog import pack
        from interview_agent.question_banks.contracts import BankRef
        from interview_agent.question_banks.repository import BankRepository
        intake = row['frozen'] or {}
        bank_refs = [BankRef.model_validate(ref) for ref in intake.get('question_banks', [])]
        role_pack = pack(row['role'])
        topics = role_pack['topics']
        async with self.pool.connection() as conn:
            goal = await (await conn.execute(
                'SELECT * FROM app.career_goals WHERE id=%s', (row['goal_id'],))).fetchone()
            target = await Materials(self.pool)._target(conn, goal)
            target_text = (target.get('requirements') or target.get('text') or target.get('direction') or '').strip()
            if bank_refs:
                selected_pack, _ = await BankRepository(self.pool).resolve(conn, row['user_id'], row['role'], bank_refs)
                topics = {**selected_pack['topics'], **topics}
            wiki = None
            if row.get('profile') == 'full' and row['role'] == 'ai-application' and not row['draft_id']:
                wiki_topic = explicit_topic(target_text[:2500])
                if wiki_topic:
                    wiki = await active_question(conn, row['user_id'], wiki_topic)
            draft = None
            if row['draft_id']:
                draft_row = await (await conn.execute(
                    'SELECT id,version,goal_version,material_revision,payload FROM app.career_material_drafts WHERE id=%s',
                    (row['draft_id'],))).fetchone()
                if draft_row:
                    draft = {'id': str(draft_row['id']), 'version': draft_row['version'],
                             'fact_count': len(draft_row['payload'].get('facts') or [])}
            selection=await (await conn.execute('''SELECT snapshot_id,carried_from_version
                FROM app.career_job_selections WHERE goal_id=%s AND goal_version=%s''',
                (row['goal_id'],row['goal_version']))).fetchone()
        target_text = target_text[:2500]
        coverage = requirements(target_text, topics) if row.get('profile') == 'full' else []
        if wiki:
            coverage = include_generated(coverage, [wiki], target['kind'])
        # Keep private question excerpts bounded in the model view. The full selection stays local.
        relevant = {topic_id for row_coverage in coverage for topic_id in row_coverage['candidates']}
        private_examples = [topic['base'] for topic_id, topic in topics.items()
                            if topic_id in relevant and topic_id not in role_pack['topics']][:6]
        question_basis = ({
            'text': target_text, 'target_sha256': digest(target),
            'existing_questions': [topic['base'] for topic in role_pack['topics'].values()]
                                  + private_examples + ([wiki['question']] if wiki else []),
            'known_questions': [topic[key] for topic in topics.values()
                                for key in ('base', 'probe') if topic.get(key)]
                               + ([wiki['question'], wiki['probe']] if wiki else []),
        } if row.get('profile') == 'full' and target_text else None)
        return {
            'goal_version': goal['version'], 'goal_status': goal['status'],
            # The planner may only cite topics from this role's frozen pack.
            'topic_order': list(role_pack['topic_order']),
            **({'question_basis': question_basis} if question_basis else {}),
            **({'wiki_question': wiki} if wiki else {}),
            **({'jd_coverage': coverage} if coverage else {}),
            **{key: intake[key] for key in ('question_banks', 'allow_project_questions') if key in intake},
            'material_revision': row['material_revision'],
            'target': {k: target[k] for k in ('kind',) if k in target}
                      | ({'title': target['title'][:120]} if target.get('title') else {})
                      | ({'direction': target['direction'][:120]} if target.get('direction') else {}),
            'draft': draft, 'resume_item_indexes': list(row['resume_item_indexes']),
            'use_resume': row['use_resume'],
            'selection': ({'snapshot_id': str(selection['snapshot_id']),
                           'carried_from_version': selection['carried_from_version']}
                          if selection else None),
            'role': row['role'], 'mode': row['mode'], 'profile': row.get('profile'),
            'max_questions': row['max_questions'],
            'duration_seconds': row['duration_seconds'],
        }
