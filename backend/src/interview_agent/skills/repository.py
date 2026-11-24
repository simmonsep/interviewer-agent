"""Storage for user-installed skills: preview, commit, list, enable, uninstall.

Versions are append-only (same id, new version). Enabling a version only affects *future*
sessions; a session freezes the body it started with.
"""
from uuid import UUID

from psycopg.types.json import Jsonb

from interview_agent.career.repository import digest
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError

from .install import parse_bundle


def summary(row: dict) -> dict:
    document = row['document']
    return {'skill_id': row['skill_id'], 'version': row['version'], 'enabled': row['enabled'],
            'revision': row['revision'], 'content_sha256': row['content_sha256'],
            'name': document['name'], 'description': document['description'],
            'author': document.get('author', ''),
            'body_chars': len(document['body']),
            'reference_count': len(document.get('references') or [])}


class SkillRepository:
    def __init__(self, pool):
        self.pool = pool

    async def _auth(self, conn, user, lock=False):
        await require_session(conn, user)
        if lock:
            await conn.execute('SELECT id FROM auth.users WHERE id=%s FOR UPDATE', (user.user_id,))

    def preview_view(self, row):
        document = row['document']
        return {'preview_id': str(row['id']), 'document': document,
                'content_sha256': row['content_sha256'],
                'requires_confirmation': True,
                'declared_tools_are_metadata_only': True,
                'review': 'user_content_not_verified'}

    async def preview(self, user, key, value):
        document = parse_bundle(value.files)
        hashed = digest(document)
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            row = await (await conn.execute(
                'SELECT * FROM app.user_skill_imports WHERE user_id=%s AND request_id=%s',
                (user.user_id, key))).fetchone()
            if row:
                if row['request_hash'] != hashed:
                    raise ApiError('idempotency_conflict', 409)
                return self.preview_view(row), False
            count = await (await conn.execute(
                'SELECT count(*) AS n FROM app.user_skill_imports WHERE user_id=%s',
                (user.user_id,))).fetchone()
            if count['n'] >= 100:
                raise ApiError('skill_preview_limit', 409)
            row = await (await conn.execute('''INSERT INTO app.user_skill_imports
                (user_id,request_id,request_hash,document,content_sha256) VALUES(%s,%s,%s,%s,%s)
                RETURNING *''', (user.user_id, key, hashed, Jsonb(document), hashed))).fetchone()
            return self.preview_view(row), True

    async def commit(self, user, preview_id, key, value):
        preview_id = str(UUID(preview_id))
        hashed = digest({'preview_id': preview_id, **value.model_dump(mode='json')})
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            old = await (await conn.execute(
                'SELECT skill_id,version,request_hash FROM app.user_skills WHERE user_id=%s AND request_id=%s',
                (user.user_id, key))).fetchone()
            if old:
                if old['request_hash'] != hashed:
                    raise ApiError('idempotency_conflict', 409)
                return {'skill_id': old['skill_id'], 'version': old['version']}, False
            row = await (await conn.execute(
                'SELECT * FROM app.user_skill_imports WHERE id=%s AND user_id=%s',
                (preview_id, user.user_id))).fetchone()
            if not row:
                raise ApiError('resource_unavailable', 404)
            document = row['document']
            latest = await (await conn.execute(
                'SELECT max(version) AS v FROM app.user_skills WHERE user_id=%s AND skill_id=%s',
                (user.user_id, document['skill_id']))).fetchone()
            version = int(latest['v'] or 0) + 1
            active = value.enabled
            if active:
                active = not await self._other_skill_active(conn, user.user_id, document['skill_id'])
            # Only one version per skill is active; a new import becomes the active one.
            await conn.execute(
                'UPDATE app.user_skills SET enabled=false, revision=revision+1 WHERE user_id=%s AND skill_id=%s',
                (user.user_id, document['skill_id']))
            await conn.execute('''INSERT INTO app.user_skills
                (user_id,skill_id,version,enabled,author,content_sha256,document,request_id,request_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (user.user_id, document['skill_id'], version, active,
                 document.get('author', ''), row['content_sha256'], Jsonb(document), key, hashed))
            return {'skill_id': document['skill_id'], 'version': version, 'enabled': active,
                    **({'note': 'another_skill_active'} if value.enabled and not active else {})}, True

    async def listing(self, user):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user)
            rows = await (await conn.execute(
                'SELECT * FROM app.user_skills WHERE user_id=%s ORDER BY skill_id, version DESC',
                (user.user_id,))).fetchall()
        skills = {}
        for row in rows:
            item = skills.setdefault(row['skill_id'], {'skill_id': row['skill_id'], 'versions': []})
            item['versions'].append(summary(row))
            if row['enabled']:
                item['active_version'] = row['version']
        for item in skills.values():
            item.setdefault('active_version', None)
            item['enabled'] = item['active_version'] is not None
        return {'skills': list(skills.values())}

    async def _other_skill_active(self, conn, user_id, skill_id) -> bool:
        """One user skill id active at a time: the prompt stays small and there is no merge order.

        Switching versions of the same skill is always allowed.
        """
        row = await (await conn.execute(
            'SELECT count(DISTINCT skill_id) AS n FROM app.user_skills WHERE user_id=%s AND enabled AND skill_id<>%s',
            (user_id, skill_id))).fetchone()
        return row['n'] >= 1

    async def set_enabled(self, user, skill_id, value):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            rows = await (await conn.execute(
                'SELECT version FROM app.user_skills WHERE user_id=%s AND skill_id=%s',
                (user.user_id, skill_id))).fetchall()
            if not rows:
                raise ApiError('resource_unavailable', 404)
            versions = {row['version'] for row in rows}
            target = value.version or max(versions)
            if target not in versions:
                raise ApiError('skill_version_unknown', 404)
            if value.enabled and await self._other_skill_active(conn, user.user_id, skill_id):
                raise ApiError('skill_single_active_limit', 409)
            await conn.execute(
                'UPDATE app.user_skills SET enabled=false, revision=revision+1 WHERE user_id=%s AND skill_id=%s AND enabled',
                (user.user_id, skill_id))
            if value.enabled:
                await conn.execute(
                    'UPDATE app.user_skills SET enabled=true, revision=revision+1 WHERE user_id=%s AND skill_id=%s AND version=%s',
                    (user.user_id, skill_id, target))
        return await self.listing(user)

    async def uninstall(self, user, skill_id):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            await conn.execute('DELETE FROM app.user_skills WHERE user_id=%s AND skill_id=%s',
                               (user.user_id, skill_id))
        return await self.listing(user)

    async def export(self, user, skill_id, version):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user)
            row = await (await conn.execute(
                'SELECT * FROM app.user_skills WHERE user_id=%s AND skill_id=%s AND version=%s',
                (user.user_id, skill_id, version))).fetchone()
        if not row:
            raise ApiError('resource_unavailable', 404)
        return {'skill_id': row['skill_id'], 'version': row['version'],
                'content_sha256': row['content_sha256'], 'document': row['document']}

    async def active_for_session(self, conn, user_id, *, limit=2):
        """Enabled skills for a new session, oldest id first so replay is deterministic.

        The text is user content: it is guidance below host policy and never grants tools.
        """
        rows = await (await conn.execute('''SELECT skill_id,version,document,content_sha256
            FROM app.user_skills WHERE user_id=%s AND enabled ORDER BY skill_id''',
            (user_id,))).fetchall()
        frozen = []
        for row in rows[:limit]:
            document = row['document']
            frozen.append({'id': 'user:' + row['skill_id'], 'version': row['version'],
                           'name': document['name'], 'agent': 'interview',
                           'modes': ['interview', 'coaching'],
                           'entry': 'references/SKILL.md', 'summary': document['description'],
                           'sha256': row['content_sha256'], 'body': document['body'],
                           'origin': 'user_install', 'author': document.get('author', '')})
        return frozen
