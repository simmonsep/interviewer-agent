"""Resume↔position binding (F07Z-152): one active version per position, with append-only history."""
import hashlib
from uuid import UUID

from interview_agent.career.repository import digest
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError


def _draft_digest(markdown: str) -> str:
    return hashlib.sha256((markdown or '').encode('utf-8')).hexdigest()


class BindingRepository:
    def __init__(self, pool):
        self.pool = pool

    async def _position(self, conn, goal_id, position_id):
        row = await (await conn.execute(
            'SELECT * FROM app.career_positions WHERE id=%s AND goal_id=%s',
            (position_id, goal_id))).fetchone()
        if not row:
            raise ApiError('resource_unavailable', 404)
        return row

    async def latest_draft(self, conn, goal_id):
        return await (await conn.execute('''SELECT id,version,markdown FROM app.career_material_drafts
            WHERE goal_id=%s ORDER BY version DESC, created_at DESC LIMIT 1''', (goal_id,))).fetchone()

    async def auto_bind(self, conn, goal_id, position_id):
        """Bind the latest available resume when a position is saved/selected, once.

        No resume means no binding (the user decides later); later resume versions never
        overwrite an existing binding.
        """
        active = await (await conn.execute(
            'SELECT 1 FROM app.career_position_bindings WHERE position_id=%s AND active',
            (position_id,))).fetchone()
        if active:
            return
        draft = await self.latest_draft(conn, goal_id)
        if not draft:
            return
        await conn.execute('''INSERT INTO app.career_position_bindings
            (goal_id,position_id,resume_draft_id,resume_version,content_sha256,active)
            VALUES(%s,%s,%s,%s,%s,true)''',
            (goal_id, position_id, draft['id'], draft['version'], _draft_digest(draft['markdown'])))

    async def view(self, conn, goal_id, position_id):
        position = await self._position(conn, goal_id, position_id)
        rows = await (await conn.execute('''SELECT b.*,d.markdown FROM app.career_position_bindings b
            JOIN app.career_material_drafts d ON d.id=b.resume_draft_id
            WHERE b.position_id=%s ORDER BY b.created_at DESC, b.id DESC''', (position_id,))).fetchall()
        history = [{'binding_id': str(row['id']), 'resume_draft_id': str(row['resume_draft_id']),
                    'resume_version': row['resume_version'], 'content_sha256': row['content_sha256'],
                    'active': row['active'], 'created_at': row['created_at'].isoformat()} for row in rows]
        drafts = await (await conn.execute('''SELECT id,version,markdown,created_at
            FROM app.career_material_drafts WHERE goal_id=%s
            ORDER BY version DESC, created_at DESC''', (goal_id,))).fetchall()
        candidates = [{'draft_id': str(row['id']), 'version': row['version'],
                       'excerpt': (row['markdown'] or '').strip().splitlines()[0][:80] if (row['markdown'] or '').strip() else '',
                       'created_at': row['created_at'].isoformat()} for row in drafts]
        return {'position_id': position_id, 'position_version': position['version'],
                'active': next((item for item in history if item['active']), None),
                'history': history, 'candidates': candidates}

    async def set(self, principal, goal_id, position_id, key, value):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            goal = await (await conn.execute('''SELECT id,version,status FROM app.career_goals
                WHERE id=%s AND user_id=%s''', (goal_id, principal.user_id))).fetchone()
            if not goal:
                raise ApiError('resource_unavailable', 404)
            if goal['status'] == 'CANCELLED':
                raise ApiError('goal_cancelled', 409)
            position = await (await conn.execute(
                'SELECT * FROM app.career_positions WHERE id=%s AND goal_id=%s FOR UPDATE',
                (position_id, goal_id))).fetchone()
            if not position:
                raise ApiError('resource_unavailable', 404)
            request_hash = digest({'position_id': str(position_id),
                                   'draft_id': str(value.draft_id),
                                   'expected_version': value.expected_version})
            prior = await (await conn.execute('''SELECT id,request_hash FROM app.career_position_bindings
                WHERE goal_id=%s AND request_id=%s''', (goal_id, key))).fetchone()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise ApiError('idempotency_conflict', 409)
                return await self.view(conn, goal_id, position_id), False
            if position['version'] != value.expected_version:
                raise ApiError('position_version_conflict', 409)
            active = await (await conn.execute('''SELECT resume_draft_id FROM app.career_position_bindings
                WHERE position_id=%s AND active''', (position_id,))).fetchone()
            if active and str(active['resume_draft_id']) == str(value.draft_id):
                return await self.view(conn, goal_id, position_id), False
            draft = await (await conn.execute('''SELECT id,version,markdown FROM app.career_material_drafts
                WHERE id=%s AND goal_id=%s''', (str(value.draft_id), goal_id))).fetchone()
            if not draft:
                raise ApiError('resource_unavailable', 404)
            await conn.execute(
                'UPDATE app.career_position_bindings SET active=false WHERE position_id=%s AND active',
                (position_id,))
            await conn.execute('''INSERT INTO app.career_position_bindings
                (goal_id,position_id,resume_draft_id,resume_version,content_sha256,request_id,request_hash,active)
                VALUES(%s,%s,%s,%s,%s,%s,%s,true)''',
                (goal_id, position_id, draft['id'], draft['version'],
                 _draft_digest(draft['markdown']), key, request_hash))
            await conn.execute('''UPDATE app.career_positions
                SET version=version+1, updated_at=clock_timestamp() WHERE id=%s''', (position_id,))
            return await self.view(conn, goal_id, position_id), True
