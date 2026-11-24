"""Import the user's uploaded text as resume v1 — no model call, no fact promotion.

The result is an immutable draft whose bullets are the source's own segments, so every line keeps
its provenance and the export rule can treat it as user-quoted text.
"""
from psycopg.types.json import Jsonb

from interview_agent.career.repository import digest
from interview_agent.storage.errors import ApiError

from .segments import scope_segments


async def import_resume(repo, principal, goal_id, key, value):
    async with repo.pool.connection() as conn, conn.transaction():
        await repo._user(conn, principal, goal_id)          # auth only
        goal, state = await repo._user(conn, principal, goal_id, value)
        request_hash = digest({'action': 'resume_import', **value.model_dump(mode='json')})
        prior = await (await conn.execute(
            'SELECT id,version,request_hash FROM app.career_material_drafts WHERE goal_id=%s AND request_id=%s',
            (goal_id, key))).fetchone()
        if prior:
            if prior['request_hash'] != request_hash:
                raise ApiError('idempotency_conflict', 409)
            return str(prior['id']), prior['version'], False
        rows = await (await conn.execute('''SELECT id,name,body,content_sha256,material_revision
            FROM app.career_material_sources WHERE goal_id=%s
            ORDER BY created_at DESC, id DESC LIMIT 2''', (goal_id,))).fetchall()
        if value.source_ids:
            wanted = {str(item) for item in value.source_ids}
            rows = [row for row in rows if str(row['id']) in wanted]
        if not rows:
            raise ApiError('resume_source_required', 400)
        sources = [{'id': str(row['id']), 'name': row['name'], 'text': row['body'],
                    'content_sha256': row['content_sha256'],
                    'material_revision': row['material_revision']} for row in rows]
        segments = scope_segments({'sources': sources})
        items = []
        for segment in segments:
            if segment.get('heading'):
                continue
            text = str(segment.get('text') or '').strip()
            if not text:
                continue
            items.append({'text': text[:500], 'source_ids': [segment['source_id']],
                          'change_note': '按原文导入', 'evidence_segments': [segment['id']]})
            if len(items) >= 60:
                break
        if not items:
            raise ApiError('resume_source_empty', 400)
        markdown = '\n\n'.join(f'- {item["text"]}' for item in items) + '\n'
        latest = await (await conn.execute(
            'SELECT max(version) AS v FROM app.career_material_drafts WHERE goal_id=%s', (goal_id,))).fetchone()
        version = int(latest['v'] or 0) + 1
        target = await repo._target(conn, goal)
        payload = {
            'draft_style': 'verbatim', 'target': target,
            'sources': sources, 'segments': segments,
            'rewrite_items': items,
            'basics': [], 'education': [], 'experience': [], 'projects': [], 'skills': [],
            'omitted_segments': [], 'structured_segment_ids': [], 'structured_sections': {},
            'truncated_segment_ids': [], 'suggestions': [], 'grounding_findings': [],
            'candidate_status': 'user_text',
            'authoring_record': {
                'origin': 'uploaded', 'parent_version': None, 'version': version,
                'sources': [{k: source[k] for k in ('id', 'name', 'content_sha256')} for source in sources],
                'items': [{**item, 'index': index} for index, item in enumerate(items)],
            },
        }
        row = await (await conn.execute('''INSERT INTO app.career_material_drafts
            (goal_id,version,goal_version,material_revision,payload,markdown,request_id,request_hash,origin)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'uploaded') RETURNING id''',
            (goal_id, version, goal['version'], state['revision'], Jsonb(payload), markdown, key, request_hash))).fetchone()
        return str(row['id']), version, True
