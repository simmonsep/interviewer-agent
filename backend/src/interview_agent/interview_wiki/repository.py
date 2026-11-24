"""Compile a used, sourced question into a reviewable Wiki version."""
from psycopg.types.json import Jsonb

from interview_agent.career.repository import digest
from interview_agent.connectors.public_reference import CATALOG
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError


def view(row):
    return {
        'id': str(row['id']), 'topic_key': row['topic_key'], 'version': row['version'],
        'status': row['status'], 'source_event_id': str(row['source_event_id']),
        'document': row['document'],
    }


async def _lock_owner(conn, user_id):
    # Serialize additions and review per owner. The partial unique index is the final publish gate.
    await conn.execute('SELECT id FROM auth.users WHERE id=%s FOR NO KEY UPDATE', (user_id,))


async def record_used_question(conn, user_id, event_id, item):
    source = item.get('public_reference')
    if not source or item.get('wiki') or item.get('experience_reference'):
        return
    topic_key = source['id']
    configured = CATALOG.get(topic_key)
    if not configured or configured.url != source['url']:
        return
    question = {
        'title': item['title'], 'question': item['base'], 'probe': item['probe'],
        'kind': item['generated']['kind'], 'difficulty': item['generated']['difficulty'],
        'public_reference': source,
    }
    # Capture time alone is not a new version. Keep it in the provenance of the first capture.
    fingerprint = digest({**question, 'public_reference': {
        key: value for key, value in source.items() if key != 'fetched_at'}})
    await _lock_owner(conn, user_id)
    await conn.execute('''
        INSERT INTO app.interview_wiki_versions
            (user_id,source_event_id,topic_key,version,content_sha256,document)
        SELECT %s,%s,%s,COALESCE(max(version),0)+1,%s,%s
        FROM app.interview_wiki_versions WHERE user_id=%s AND topic_key=%s
        ON CONFLICT (user_id,topic_key,content_sha256) DO NOTHING
    ''', (user_id, event_id, topic_key, fingerprint, Jsonb(question), user_id, topic_key))


async def active_question(conn, user_id, key):
    row = await (await conn.execute('''
        SELECT * FROM app.interview_wiki_versions
        WHERE user_id=%s AND topic_key=%s AND status='active'
    ''', (user_id, key))).fetchone()
    if row is None:
        return None
    return {**row['document'], 'target_quote': '',
            'wiki': {'id': str(row['id']), 'version': row['version'], 'topic_key': row['topic_key']}}


async def usable(conn, user_id, identity):
    # A newer publication archives this version. Explicit withdrawal invalidates future use.
    row = await (await conn.execute('''
        SELECT id FROM app.interview_wiki_versions
        WHERE user_id=%s AND id=%s AND status IN ('active','archived') FOR SHARE
    ''', (user_id, identity))).fetchone()
    return row is not None


class WikiRepository:
    def __init__(self, pool):
        self.pool = pool

    async def listing(self, principal):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            rows = await (await conn.execute('''
                SELECT * FROM app.interview_wiki_versions WHERE user_id=%s
                ORDER BY topic_key,version DESC
            ''', (principal.user_id,))).fetchall()
            return {'items': [view(row) for row in rows]}

    async def review(self, principal, identity, decision):
        async with self.pool.connection() as conn, conn.transaction():
            await require_session(conn, principal)
            await _lock_owner(conn, principal.user_id)
            row = await (await conn.execute('''
                SELECT * FROM app.interview_wiki_versions WHERE id=%s AND user_id=%s
            ''', (identity, principal.user_id))).fetchone()
            if row is None:
                raise ApiError('resource_unavailable', 404)
            status = 'withdrawn' if decision.action == 'withdraw' else 'active'
            if row['status'] == status:
                return view(row)
            active = await (await conn.execute('''
                SELECT id FROM app.interview_wiki_versions
                WHERE user_id=%s AND topic_key=%s AND status='active'
            ''', (principal.user_id, row['topic_key']))).fetchone()
            active_id = str(active['id']) if active else None
            if active_id != decision.expected_active_id:
                raise ApiError('wiki_version_conflict', 409)
            if decision.action != 'withdraw':
                required = 'pending' if decision.action == 'publish' else 'archived'
                if row['status'] != required:
                    raise ApiError('wiki_version_not_reviewable', 409)
                if active:
                    await conn.execute("UPDATE app.interview_wiki_versions SET status='archived' WHERE id=%s",
                                       (active['id'],))
            updated = await (await conn.execute('''
                UPDATE app.interview_wiki_versions SET status=%s WHERE id=%s RETURNING *
            ''', (status, identity))).fetchone()
            return view(updated)
