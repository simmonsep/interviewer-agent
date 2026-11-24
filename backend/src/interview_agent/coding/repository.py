"""SQL authority for user versions, assistant proposals and fenced coding execution."""
from copy import deepcopy
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.storage.operations import Claim, duration
from interview_agent.career.repository import digest
from interview_agent.materials.repository import MaterialRepository
from .catalog import task, public_task
from .workspace import Workspace
from .agent import validate_result

RUNNING = ('PENDING', 'RUNNING')


def restore(payload):
    w = Workspace(payload['task']); w.files = deepcopy(payload['files'])
    return w


class CodingRepository:
    def __init__(self, pool, image=None, *, assistant_mode='scripted'):
        self.pool, self.image, self.assistant_mode = pool, image, assistant_mode

    async def _root(self, conn, gid, user=None):
        row = await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s' + (' AND user_id=%s' if user else '') + ' FOR UPDATE',
                                       (gid, user) if user else (gid,))).fetchone()
        if not row: raise ApiError('resource_unavailable', 404)
        return row

    async def _cancel(self, conn, sid, reason):
        await conn.execute("""UPDATE app.career_coding_runs SET status='CANCELLED',lease_until=NULL,fencing_token=fencing_token+1,error_code=%s
            WHERE session_id=%s AND status IN ('PENDING','RUNNING')""", (reason, sid))

    async def _session(self, conn, gid, sid, user=None):
        g = await self._root(conn, gid, user)
        material = await MaterialRepository(self.pool)._state(conn, gid)
        s = await (await conn.execute('SELECT * FROM app.career_coding_sessions WHERE id=%s AND goal_id=%s FOR UPDATE', (sid, gid))).fetchone()
        if not s: raise ApiError('resource_unavailable', 404)
        if s['status'] in ('ACTIVE', 'SUBMITTED'):
            target = await MaterialRepository(self.pool)._target(conn, g)
            if (g['status'] == 'CANCELLED' or g['version'] != s['goal_version']
                    or material['revision'] != s['input_payload']['material_revision'] or digest(target) != digest(s['input_payload']['target'])):
                await conn.execute("UPDATE app.career_coding_sessions SET status='STALE',stop_reason='context_changed',version=version+1,revision=revision+1 WHERE id=%s", (sid,))
                await self._cancel(conn, sid, 'context_changed')
            elif s['status'] == 'ACTIVE':
                changed = await (await conn.execute("""UPDATE app.career_coding_sessions SET status='EXPIRED',stop_reason='deadline',version=version+1,revision=revision+1
                    WHERE id=%s AND deadline_at<=clock_timestamp() RETURNING id""", (sid,))).fetchone()
                if changed: await self._cancel(conn, sid, 'deadline')
            s = await (await conn.execute('SELECT * FROM app.career_coding_sessions WHERE id=%s', (sid,))).fetchone()
        return g, s

    async def _version(self, conn, s):
        return await (await conn.execute('SELECT * FROM app.career_coding_versions WHERE session_id=%s AND revision=%s', (s['id'], s['workspace_revision']))).fetchone()

    async def _idle(self, conn, s):
        if await (await conn.execute("SELECT id FROM app.career_coding_runs WHERE session_id=%s AND status IN ('PENDING','RUNNING') LIMIT 1", (s['id'],))).fetchone():
            raise ApiError('coding_run_in_progress', 409)

    async def _active(self, conn, s, value):
        if s['status'] != 'ACTIVE' or s['version'] != value.expected_version: raise ApiError('version_conflict', 409)
        v = await self._version(conn, s)
        if v['files_sha256'] != value.expected_workspace_sha256: raise ApiError('coding_workspace_conflict', 409)
        await self._idle(conn, s)
        return v

    async def _prior(self, conn, table, sid, key, hashed):
        # Table identifiers are internal literals, never supplied by HTTP or the model.
        r = await (await conn.execute(f'SELECT id,request_hash FROM app.{table} WHERE session_id=%s AND request_id=%s', (sid, key))).fetchone()
        if r and r['request_hash'] != hashed: raise ApiError('idempotency_conflict', 409)
        return r

    async def create(self, principal, gid, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); g = await self._root(conn, gid, principal.user_id)
            if self.image is None: raise ApiError('coding_executor_disabled', 503)
            hashed = digest(value.model_dump(mode='json'))
            prior = await (await conn.execute('SELECT id,request_hash FROM app.career_coding_sessions WHERE goal_id=%s AND request_id=%s', (gid, key))).fetchone()
            if prior:
                if prior['request_hash'] != hashed: raise ApiError('idempotency_conflict', 409)
                return str(prior['id']), False
            if g['version'] != value.expected_version or g['status'] == 'CANCELLED': raise ApiError('version_conflict', 409)
            old = await (await conn.execute('SELECT id FROM app.career_coding_sessions WHERE goal_id=%s ORDER BY created_at', (gid,))).fetchall()
            if len(old) >= 10 or g['coding_runs'] >= 24: raise ApiError('coding_session_limit', 409)
            for row in old:
                _, s = await self._session(conn, gid, row['id'])
                if s['status'] in ('ACTIVE', 'SUBMITTED'): raise ApiError('coding_session_in_progress', 409)
            material = await MaterialRepository(self.pool)._state(conn, gid)
            t = task(value.task_id)
            policy = {'version': 'ja07-assisted-session-v2', 'mode': value.mode, 'system_ai_help': True,
                      'image': self.image, 'execution_policy': 'ja06-docker-java-v1', 'workspace_policy': 'ja07-flat-java-v1',
                      'max_assist_runs': 2 if value.mode == 'practice' else 1, 'max_public_tests_per_assist': 3,
                      'max_model_responses_per_assist': 12, 'max_executions': 12, 'max_runs': 8,
                      'provider_capability': self.assistant_mode, 'assistant_provider': self.assistant_mode,
                      'independent_ability_certified': False, 'external_assistance_verified': False}
            payload = {'task': t, 'target': await MaterialRepository(self.pool)._target(conn, g),
                       'material_revision': material['revision'], 'policy': policy}
            s = await (await conn.execute('''INSERT INTO app.career_coding_sessions(goal_id,goal_version,request_id,request_hash,input_payload,deadline_at)
                VALUES(%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING *''',
                (gid, g['version'], key, hashed, Jsonb(payload), value.duration_seconds))).fetchone()
            await self._append_version(conn, s, t['files'], [], 'starter', '固定初始仓库', None, initial=True)
            return str(s['id']), True

    async def _append_version(self, conn, s, files, edits, origin, explanation, source_run_id, *, initial=False):
        n = 1 if initial else s['workspace_revision'] + 1
        if n > 12: raise ApiError('coding_version_limit', 409)
        await conn.execute('''INSERT INTO app.career_coding_versions(session_id,revision,origin,files,files_sha256,edits,explanation,source_run_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (s['id'], n, origin, Jsonb(files), digest(files), Jsonb(edits), explanation, source_run_id))
        if not initial:
            await conn.execute('UPDATE app.career_coding_sessions SET workspace_revision=%s,version=version+1,revision=revision+1 WHERE id=%s', (n, s['id']))

    async def _command(self, conn, s, key, hashed, kind, value):
        await conn.execute('''INSERT INTO app.career_coding_commands(session_id,kind,request_id,request_hash,input_payload)
            VALUES(%s,%s,%s,%s,%s)''', (s['id'], kind, key, hashed, Jsonb(value.model_dump(mode='json'))))

    async def edit(self, principal, gid, sid, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); _, s = await self._session(conn, gid, sid, principal.user_id)
            hashed = digest({'kind': 'edit', **value.model_dump(mode='json')})
            if await self._prior(conn, 'career_coding_commands', sid, key, hashed): return False
            v = await self._active(conn, s, value); w = restore({'task': s['input_payload']['task'], 'files': v['files']})
            try: change = w.replace(value.path, value.expected_sha256, value.text, actor='user')
            except ValueError as error: raise ApiError(str(error), 409 if str(error) == 'file_version_conflict' else 400) from None
            if not change['changed']: raise ApiError('coding_no_change', 400)
            await self._append_version(conn, s, w.files, w.edits, 'user', value.explanation, None)
            await self._command(conn, s, key, hashed, 'edit', value)
            return True

    async def enqueue(self, principal, gid, sid, key, value, kind):
        if kind not in ('public', 'assist', 'final'): raise ValueError('invalid_coding_kind')
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); g, s = await self._session(conn, gid, sid, principal.user_id)
            if self.image is None: raise ApiError('coding_executor_disabled', 503)
            if self.image != s['input_payload']['policy']['image']: raise ApiError('coding_image_changed', 409)
            hashed = digest({'kind': kind, **value.model_dump(mode='json')})
            prior = await self._prior(conn, 'career_coding_runs', sid, key, hashed)
            if prior: return str(prior['id']), False
            v = await self._active(conn, s, value)
            if s['runs'] >= 8 or g['coding_runs'] >= 24 or s['executions'] >= 12 or g['coding_executions'] >= 36:
                raise ApiError('coding_budget_exhausted', 409)
            if kind == 'assist' and (s['assist_runs'] >= s['input_payload']['policy']['max_assist_runs'] or g['coding_assistant_responses'] >= 48):
                raise ApiError('coding_assistant_budget_exhausted', 409)
            payload = {**s['input_payload'], 'files': v['files'], 'files_sha256': v['files_sha256'],
                       'workspace_revision': v['revision'], 'user_explanation': getattr(value, 'explanation', '')}
            r = await (await conn.execute('''INSERT INTO app.career_coding_runs(goal_id,goal_version,session_id,kind,workspace_revision,request_id,request_hash,input_payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (gid, g['version'], sid, kind, v['revision'], key, hashed, Jsonb(payload)))).fetchone()
            await conn.execute("UPDATE app.career_coding_sessions SET runs=runs+1,assist_runs=assist_runs+%s,version=version+1,revision=revision+1,status=%s WHERE id=%s",
                               (1 if kind == 'assist' else 0, 'SUBMITTED' if kind == 'final' else 'ACTIVE', sid))
            await conn.execute('UPDATE app.career_goals SET coding_runs=coding_runs+1,revision=revision+1 WHERE id=%s', (gid,))
            return str(r['id']), True

    async def decide(self, principal, gid, sid, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); _, s = await self._session(conn, gid, sid, principal.user_id)
            hashed = digest({'kind': value.decision, **value.model_dump(mode='json')})
            if await self._prior(conn, 'career_coding_commands', sid, key, hashed): return False
            v = await self._active(conn, s, value)
            run = await (await conn.execute("SELECT * FROM app.career_coding_runs WHERE id=%s AND session_id=%s AND kind='assist'", (value.run_id, sid))).fetchone()
            if not run: raise ApiError('resource_unavailable', 404)
            if run['status'] != 'SUCCEEDED': raise ApiError('coding_proposal_unavailable', 409)
            prior = await (await conn.execute("SELECT id FROM app.career_coding_commands WHERE session_id=%s AND kind IN ('accept','reject') AND input_payload->>'run_id'=%s", (sid, value.run_id))).fetchone()
            if prior: raise ApiError('coding_proposal_already_decided', 409)
            if value.decision == 'accept':
                if run['workspace_revision'] != s['workspace_revision'] or run['input_payload']['files_sha256'] != v['files_sha256']:
                    raise ApiError('coding_proposal_stale', 409)
                proposed = run['result']['proposal']['snapshot']
                if proposed['workspace_sha256'] == v['files_sha256']: raise ApiError('coding_no_change', 400)
                await self._append_version(conn, s, proposed['files'], proposed['edits'], 'assistant', run['result']['proposal']['explanation'], run['id'])
            else:
                await conn.execute('UPDATE app.career_coding_sessions SET version=version+1,revision=revision+1 WHERE id=%s', (sid,))
            await self._command(conn, s, key, hashed, value.decision, value)
            return True

    async def cancel(self, principal, gid, sid, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); _, s = await self._session(conn, gid, sid, principal.user_id)
            hashed = digest({'kind': 'cancel', **value.model_dump(mode='json')})
            if await self._prior(conn, 'career_coding_commands', sid, key, hashed): return False
            if s['version'] != value.expected_version or s['status'] not in ('ACTIVE', 'SUBMITTED'): raise ApiError('version_conflict', 409)
            await self._cancel(conn, sid, 'user_cancel')
            await conn.execute("UPDATE app.career_coding_sessions SET status='CANCELLED',stop_reason='user_cancel',version=version+1,revision=revision+1 WHERE id=%s", (sid,))
            await self._command(conn, s, key, hashed, 'cancel', value)
            return True

    async def listing(self, principal, gid):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); await self._root(conn, gid, principal.user_id)
            rows = await (await conn.execute('SELECT id FROM app.career_coding_sessions WHERE goal_id=%s ORDER BY created_at DESC LIMIT 10', (gid,))).fetchall()
        return [await self.get(principal, gid, str(r['id'])) for r in rows]

    async def get(self, principal, gid, sid):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal); g, s = await self._session(conn, gid, sid, principal.user_id)
            versions = await (await conn.execute('SELECT * FROM app.career_coding_versions WHERE session_id=%s ORDER BY revision', (sid,))).fetchall()
            runs = await (await conn.execute('SELECT * FROM app.career_coding_runs WHERE session_id=%s ORDER BY created_at,id', (sid,))).fetchall()
            commands = await (await conn.execute('SELECT kind,input_payload FROM app.career_coding_commands WHERE session_id=%s ORDER BY created_at,id', (sid,))).fetchall()
            current = next(v for v in versions if v['revision'] == s['workspace_revision'])
            return {'id': str(s['id']), 'goal_version': s['goal_version'], 'version': s['version'], 'revision': s['revision'],
                    'status': s['status'], 'deadline_at': s['deadline_at'], 'stop_reason': s['stop_reason'],
                    'task': public_task(s['input_payload']['task']), 'policy': s['input_payload']['policy'],
                    'material_revision': s['input_payload']['material_revision'], 'target': s['input_payload']['target'],
                    'workspace': {'revision': current['revision'], 'files': current['files'], 'sha256': current['files_sha256']},
                    'versions': [{k: v[k] for k in ('revision', 'origin', 'files', 'files_sha256', 'edits', 'explanation')} for v in versions],
                    'commands': commands,
                    'runs': [{'id': str(r['id']), **{k: r[k] for k in ('kind', 'workspace_revision', 'status', 'attempt', 'executions', 'assistant_responses', 'result', 'error_code')},
                              'user_explanation': r['input_payload']['user_explanation'], 'origin': r['origin'], 'model_requests': r['model_requests']} for r in runs],
                    'budget': {k: s[k] for k in ('runs', 'assist_runs', 'executions', 'assistant_responses')},
                    'goal_budget': {k: g[k] for k in ('coding_runs', 'coding_executions', 'coding_assistant_responses', 'coding_model_requests')},
                    'ability_certified': False, 'external_assistance_verified': False}

    async def _lock(self, conn, message):
        ref = await (await conn.execute('SELECT goal_id,session_id FROM app.career_coding_runs WHERE id=%s AND job_id=%s', (message.operation_id, message.job_id))).fetchone()
        if not ref: return None
        g, s = await self._session(conn, ref['goal_id'], ref['session_id'])
        r = await (await conn.execute('SELECT * FROM app.career_coding_runs WHERE id=%s FOR UPDATE', (message.operation_id,))).fetchone()
        return g, s, r

    async def _owned(self, conn, claim):
        rows = await self._lock(conn, claim.message)
        if not rows: return None
        g, s, r = rows; now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if (s['status'] not in ('ACTIVE', 'SUBMITTED') or r['status'] != 'RUNNING' or r['attempt'] != claim.attempt
                or r['fencing_token'] != claim.fencing_token or r['lease_until'] <= now
                or r['goal_version'] != claim.message.resource_version or str(g['user_id']) != claim.user_id
                or str(g['id']) != claim.resource_id or s['workspace_revision'] != r['workspace_revision']): return None
        return rows

    async def pending_dispatch(self, recovery_seconds=5):
        async with self.pool.connection() as conn:
            rows = await (await conn.execute("SELECT id,goal_id FROM app.career_coding_sessions WHERE status IN ('ACTIVE','SUBMITTED') ORDER BY created_at LIMIT 100")).fetchall()
            for s in rows:
                async with conn.transaction(): await self._session(conn, s['goal_id'], s['id'])
            return await (await conn.execute("""SELECT id AS outbox_id,id AS operation_id,job_id,goal_version AS resource_version FROM app.career_coding_runs
                WHERE (status='PENDING' OR (status='RUNNING' AND lease_until<=clock_timestamp()))
                AND (dispatched_at IS NULL OR dispatched_at<=clock_timestamp()-(%s*interval '1 second')) ORDER BY created_at LIMIT 100""", (recovery_seconds,))).fetchall()

    async def mark_dispatched(self, identity):
        async with self.pool.connection() as conn:
            await conn.execute('UPDATE app.career_coding_runs SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=%s', (identity,))

    async def claim(self, message, lease_seconds=6):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._lock(conn, message)
            if not rows: return None
            g, s, r = rows; now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
            if (s['status'] not in ('ACTIVE', 'SUBMITTED') or r['status'] not in RUNNING or r['goal_version'] != message.resource_version
                    or (r['status'] == 'RUNNING' and r['lease_until'] > now)): return None
            if r['attempt'] >= 3 or (r['started'] and r['candidate'] is None):
                await self._fail(conn, s, r, 'coding_outcome_unknown'); return None
            r = await (await conn.execute("""UPDATE app.career_coding_runs SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,
                lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s RETURNING *""", (duration(lease_seconds), r['id']))).fetchone()
            return Claim(message, str(g['user_id']), str(g['id']), r['attempt'], r['fencing_token'], '', kind='coding_execution', session_id=str(s['id']))

    async def _fail(self, conn, s, r, code):
        await conn.execute("UPDATE app.career_coding_runs SET status='FAILED',lease_until=NULL,error_code=%s WHERE id=%s", (code, r['id']))
        if r['kind'] == 'final':
            await conn.execute("UPDATE app.career_coding_sessions SET status='COMPLETED',stop_reason=%s,version=version+1,revision=revision+1 WHERE id=%s", (code, s['id']))

    async def heartbeat(self, claim, lease_seconds=6):
        async with self.pool.connection() as conn, conn.transaction():
            if not await self._owned(conn, claim): return False
            await conn.execute("UPDATE app.career_coding_runs SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s", (duration(lease_seconds), claim.message.operation_id)); return True

    async def context(self, claim):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._owned(conn, claim)
            if not rows: raise ValueError('coding_claim_expired')
            return rows[2]

    async def begin(self, claim, origin=None):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._owned(conn, claim)
            if not rows or rows[2]['started']: return False
            await conn.execute('UPDATE app.career_coding_runs SET started=true,origin=%s WHERE id=%s', (origin, claim.message.operation_id)); return True

    async def admit(self, claim, kind):
        if kind not in ('execution', 'response'): raise ValueError('invalid_coding_admission')
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._owned(conn, claim)
            if not rows: raise ValueError('coding_claim_expired')
            g, s, r = rows
            if kind == 'execution':
                if (g['coding_executions'] >= 36 or s['executions'] >= 12 or r['executions'] >= (3 if r['kind'] == 'assist' else 1)):
                    raise ValueError('coding_execution_budget_exhausted')
                cols = ('coding_executions', 'executions')
            else:
                if r['kind'] != 'assist' or g['coding_assistant_responses'] >= 48 or s['assistant_responses'] >= 24 or r['assistant_responses'] >= 12:
                    raise ValueError('coding_assistant_budget_exhausted')
                cols = ('coding_assistant_responses', 'assistant_responses')
            await conn.execute(f'UPDATE app.career_goals SET {cols[0]}={cols[0]}+1,revision=revision+1 WHERE id=%s', (g['id'],))
            await conn.execute(f'UPDATE app.career_coding_sessions SET {cols[1]}={cols[1]}+1,revision=revision+1 WHERE id=%s', (s['id'],))
            await conn.execute(f'UPDATE app.career_coding_runs SET {cols[1]}={cols[1]}+1 WHERE id=%s', (r['id'],))

    def validate(self, row, result):
        p = row['input_payload']; t = p['task']
        if not isinstance(result, dict): raise ValueError('invalid_coding_candidate')
        def execution_policy(grade):
            e = grade.get('evidence', {})
            if (e.get('image') != p['policy']['image'] or e.get('policy') != p['policy']['execution_policy']
                    or e.get('workspace_policy') != p['policy']['workspace_policy']):
                raise ValueError('coding_execution_policy_mismatch')
        if row['kind'] == 'assist':
            c = result['proposal']; s = c['snapshot']
            if (c['task_sha256'] != t['sha256'] or s['task_sha256'] != t['sha256'] or digest(s['files']) != s['workspace_sha256']
                    or set(s['files']) != set(t['files']) or not c['assisted']): raise ValueError('coding_proposal_binding')
            if any(s['files'][k] != v for k, v in t['files'].items() if k not in t['editable']): raise ValueError('coding_readonly_file_changed')
            # Revalidate editable content through the same bounded workspace contract.
            w = restore(p)
            for k in t['editable']:
                if w.files[k] != s['files'][k]: w.replace(k, w.read(k)['sha256'], s['files'][k], actor='assistant')
            if not 1 <= c['model_responses'] == row['assistant_responses']: raise ValueError('coding_response_evidence_missing')
            if len(c['tests']) != row['executions']: raise ValueError('coding_execution_evidence_missing')
            if c['status'] not in ('verified', 'needs_work'): raise ValueError('invalid_coding_candidate')
            current_passes = [x for x in c['tests'] if x['workspace_sha256'] == s['workspace_sha256'] and x.get('result', {}).get('verdict') == 'ACCEPTED']
            if c['status'] == 'verified' and not current_passes:
                raise ValueError('current_passing_tests_required')
            for x in current_passes:
                validate_result({k: v for k, v in s['files'].items() if k.endswith('.java')},
                                [c for c in t['cases'] if c['visibility'] == 'public'], x['result'])
            for x in c['tests']:
                # Intermediate snapshots are not final code; executor binding is recorded at tool time.
                if x.get('status') != 'COMPLETED' or any(k['visibility'] != 'public' for k in x['result'].get('cases', [])):
                    raise ValueError('coding_public_result_required')
                execution_policy(x['result'])
        else:
            cases = [c for c in t['cases'] if row['kind'] == 'final' or c['visibility'] == 'public']
            sources = {k: v for k, v in p['files'].items() if k.endswith('.java')}
            validate_result(sources, cases, result['grade'])
            execution_policy(result['grade'])
            if result.get('workspace_sha256') != p['files_sha256']: raise ValueError('coding_candidate_stale')
            if row['executions'] != 1: raise ValueError('coding_execution_evidence_missing')
        return result

    async def save(self, claim, result):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._owned(conn, claim)
            if not rows: return False
            checked = self.validate(rows[2], result)
            await conn.execute('UPDATE app.career_coding_runs SET candidate=%s WHERE id=%s', (Jsonb(checked), claim.message.operation_id)); return True

    async def complete(self, claim):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._owned(conn, claim)
            if not rows: return False
            _, s, r = rows; result = self.validate(r, r['candidate'])
            verdict = result.get('grade', {}).get('verdict')
            failed = verdict in ('ENVIRONMENT_ERROR', 'CANCELLED')
            await conn.execute('UPDATE app.career_coding_runs SET status=%s,lease_until=NULL,result=%s,error_code=%s WHERE id=%s',
                               ('FAILED' if failed else 'SUCCEEDED', Jsonb(result), 'coding_environment_error' if failed else None, r['id']))
            if r['kind'] == 'final':
                await conn.execute("UPDATE app.career_coding_sessions SET status='COMPLETED',stop_reason=%s,version=version+1,revision=revision+1 WHERE id=%s",
                                   ('final_environment_error' if failed else 'user_submission', s['id']))
            else: await conn.execute('UPDATE app.career_coding_sessions SET revision=revision+1 WHERE id=%s', (s['id'],))
            return True

    async def fail_attempt(self, claim, code='coding_worker_failed', diagnostic=None):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._owned(conn, claim)
            if not rows: return False
            await self._fail(conn, rows[1], rows[2], code)
            if diagnostic is not None:
                await conn.execute('UPDATE app.career_coding_runs SET result=%s WHERE id=%s', (Jsonb({'failure': diagnostic}), rows[2]['id']))
            return True

    async def can_ack(self, message):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await self._lock(conn, message)
            return not rows or rows[2]['status'] not in RUNNING
