"""Storage for user strategy sets and the per-session card list."""
from uuid import UUID

from psycopg.types.json import Jsonb

from interview_agent.career.repository import digest
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError

from .install import parse_document


def summary(row: dict) -> dict:
    document = row['document']
    return {'strategy_set_id': row['strategy_set_id'], 'role': row['role'],
            'version': row['version'], 'enabled': row['enabled'], 'revision': row['revision'],
            'content_sha256': row['content_sha256'], 'name': document['name'],
            'author': document.get('author', ''), 'card_count': len(document['cards'])}


class StrategyRepository:
    def __init__(self, pool):
        self.pool = pool

    async def _auth(self, conn, user, lock=False):
        await require_session(conn, user)
        if lock:
            await conn.execute('SELECT id FROM auth.users WHERE id=%s FOR UPDATE', (user.user_id,))

    def preview_view(self, row):
        document = row['document']
        return {'preview_id': str(row['id']), 'document': document,
                'content_sha256': row['content_sha256'], 'requires_confirmation': True,
                'review': 'user_content_not_verified',
                'scope': 'follow_up_guidance_not_questions'}

    async def preview(self, user, key, value):
        document = parse_document(value.document)
        hashed = digest(document)
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            row = await (await conn.execute(
                'SELECT * FROM app.user_strategy_imports WHERE user_id=%s AND request_id=%s',
                (user.user_id, key))).fetchone()
            if row:
                if row['request_hash'] != hashed:
                    raise ApiError('idempotency_conflict', 409)
                return self.preview_view(row), False
            count = await (await conn.execute(
                'SELECT count(*) AS n FROM app.user_strategy_imports WHERE user_id=%s',
                (user.user_id,))).fetchone()
            if count['n'] >= 100:
                raise ApiError('strategy_preview_limit', 409)
            row = await (await conn.execute('''INSERT INTO app.user_strategy_imports
                (user_id,request_id,request_hash,document,content_sha256) VALUES(%s,%s,%s,%s,%s)
                RETURNING *''', (user.user_id, key, hashed, Jsonb(document), hashed))).fetchone()
            return self.preview_view(row), True

    async def commit(self, user, preview_id, key, value):
        preview_id = str(UUID(preview_id))
        hashed = digest({'preview_id': preview_id, **value.model_dump(mode='json')})
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            old = await (await conn.execute(
                'SELECT strategy_set_id,version,request_hash FROM app.user_strategies WHERE user_id=%s AND request_id=%s',
                (user.user_id, key))).fetchone()
            if old:
                if old['request_hash'] != hashed:
                    raise ApiError('idempotency_conflict', 409)
                return {'strategy_set_id': old['strategy_set_id'], 'version': old['version']}, False
            row = await (await conn.execute(
                'SELECT * FROM app.user_strategy_imports WHERE id=%s AND user_id=%s',
                (preview_id, user.user_id))).fetchone()
            if not row:
                raise ApiError('resource_unavailable', 404)
            document = row['document']
            set_id = (document.get('id') or document['name']).strip().lower()
            from .contracts import SLUG
            if not SLUG.match(set_id):
                set_id = set_id.replace(' ', '-')
            if not SLUG.match(set_id):
                raise ApiError('strategy_set_id_invalid', 400)
            latest = await (await conn.execute(
                'SELECT max(version) AS v FROM app.user_strategies WHERE user_id=%s AND strategy_set_id=%s',
                (user.user_id, set_id))).fetchone()
            version = int(latest['v'] or 0) + 1
            # One enabled version per set; a new role set is enabled unless the same role already has
            # an enabled set (then it is stored disabled and the UI explains why).
            await conn.execute(
                'UPDATE app.user_strategies SET enabled=false, revision=revision+1 WHERE user_id=%s AND strategy_set_id=%s',
                (user.user_id, set_id))
            enabled = value.enabled and not await self._role_set_active(conn, user.user_id, document['role'])
            await conn.execute('''INSERT INTO app.user_strategies
                (user_id,strategy_set_id,role,version,enabled,author,content_sha256,document,request_id,request_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (user.user_id, set_id, document['role'], version, enabled,
                 document.get('author', ''), row['content_sha256'], Jsonb(document), key, hashed))
            return {'strategy_set_id': set_id, 'version': version, 'enabled': enabled,
                    **({'note': 'another_set_active'} if value.enabled and not enabled else {})}, True

    async def _role_set_active(self, conn, user_id, role) -> bool:
        row = await (await conn.execute(
            'SELECT count(*) AS n FROM app.user_strategies WHERE user_id=%s AND role=%s AND enabled',
            (user_id, role))).fetchone()
        return row['n'] >= 1

    async def listing(self, user):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user)
            rows = await (await conn.execute(
                'SELECT * FROM app.user_strategies WHERE user_id=%s ORDER BY strategy_set_id, version DESC',
                (user.user_id,))).fetchall()
        sets = {}
        for row in rows:
            item = sets.setdefault(row['strategy_set_id'], {'strategy_set_id': row['strategy_set_id'],
                                                           'role': row['role'], 'versions': []})
            item['versions'].append(summary(row))
            if row['enabled']:
                item['active_version'] = row['version']
        for item in sets.values():
            item.setdefault('active_version', None)
            item['enabled'] = item['active_version'] is not None
        return {'strategy_sets': list(sets.values())}

    async def set_enabled(self, user, set_id, value):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            rows = await (await conn.execute(
                'SELECT version,role FROM app.user_strategies WHERE user_id=%s AND strategy_set_id=%s',
                (user.user_id, set_id))).fetchall()
            if not rows:
                raise ApiError('resource_unavailable', 404)
            versions = {row['version'] for row in rows}
            target = value.version or max(versions)
            if target not in versions:
                raise ApiError('strategy_version_unknown', 404)
            role = next(row['role'] for row in rows if row['version'] == target)
            if value.enabled and await self._role_set_active(conn, user.user_id, role):
                raise ApiError('strategy_single_set_limit', 409)
            await conn.execute(
                'UPDATE app.user_strategies SET enabled=false, revision=revision+1 WHERE user_id=%s AND strategy_set_id=%s AND enabled',
                (user.user_id, set_id))
            if value.enabled:
                await conn.execute(
                    'UPDATE app.user_strategies SET enabled=true, revision=revision+1 WHERE user_id=%s AND strategy_set_id=%s AND version=%s',
                    (user.user_id, set_id, target))
        return await self.listing(user)

    async def uninstall(self, user, set_id):
        async with self.pool.connection() as conn, conn.transaction():
            await self._auth(conn, user, True)
            await conn.execute('DELETE FROM app.user_strategies WHERE user_id=%s AND strategy_set_id=%s',
                               (user.user_id, set_id))
        return await self.listing(user)

    async def active_cards(self, conn, user_id, role) -> list[dict]:
        """Enabled user cards for this role, in a stable order, marked as user content."""
        rows = await (await conn.execute('''SELECT strategy_set_id,version,author,document
            FROM app.user_strategies WHERE user_id=%s AND role=%s AND enabled
            ORDER BY strategy_set_id''', (user_id, role))).fetchall()
        cards = []
        for row in rows:
            for card in row['document']['cards']:
                cards.append({**card, 'origin': 'user_install',
                              'author': row['author'], 'set_id': row['strategy_set_id'],
                              'set_version': row['version']})
        return cards
