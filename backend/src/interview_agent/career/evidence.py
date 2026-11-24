"""F07Z-152 phase 3: detail projections for the four evidence kinds.

Read-only and goal-scoped. Interview observations obey the same terminal-visibility rule as the
session endpoint: during a live interview the detail does not exist yet.
"""
from interview_agent.storage.errors import ApiError


async def material_fact(conn, goal_id, fact_id):
    row = await (await conn.execute('''SELECT f.*,s.name AS source_name,s.body AS source_text,
            s.content_sha256 AS source_sha
        FROM app.career_material_facts f
        JOIN app.career_material_sources s ON s.id=f.source_id
        WHERE f.id=%s AND f.goal_id=%s''', (fact_id, goal_id))).fetchone()
    if not row:
        raise ApiError('resource_unavailable', 404)
    # Which resume bullets cite a segment that contains this fact's quote. This is a link, not a
    # claim that the bullet is faithful; the bullet detail carries its own guard result.
    used_by = []
    drafts = await (await conn.execute('''SELECT id,version,payload FROM app.career_material_drafts
        WHERE goal_id=%s ORDER BY version DESC''', (goal_id,))).fetchall()
    quote = str(row['quote'] or '').strip()
    for draft in drafts:
        payload = draft['payload'] or {}
        segments = {segment['id']: str(segment.get('text') or '')
                    for segment in payload.get('segments') or []}
        for index, item in enumerate(payload.get('rewrite_items') or []):
            for segment_id in item.get('evidence_segments') or []:
                text = segments.get(segment_id, '')
                if quote and (quote in text or text.strip() == quote):
                    used_by.append({'draft_id': str(draft['id']), 'version': draft['version'],
                                    'index': index})
                    break
    return {'kind': 'material_fact', 'id': str(row['id']), 'goal_id': str(row['goal_id']),
            'text': row['quote'], 'fact_kind': row['kind'],
            'source': {'id': str(row['source_id']), 'name': row['source_name'],
                       'content_sha256': row['source_sha'], 'quote': row['quote'],
                       'start_char': row['start_char'], 'end_char': row['end_char']},
            'confirmation_id': str(row['confirmation_id']),
            'used_by': used_by,
            'links': {'goal': str(goal_id)}}


async def resume_bullet(conn, goal_id, draft_id, index):
    row = await (await conn.execute('''SELECT id,version,payload,origin FROM app.career_material_drafts
        WHERE id=%s AND goal_id=%s''', (draft_id, goal_id))).fetchone()
    if not row:
        raise ApiError('resource_unavailable', 404)
    payload = row['payload'] or {}
    items = payload.get('rewrite_items') or []
    if index < 0 or index >= len(items):
        raise ApiError('resource_unavailable', 404)
    item = items[index]
    segments = {segment['id']: segment for segment in payload.get('segments') or []}
    flagged = {str(finding.get('text') or '') for finding in payload.get('grounding_findings') or []}
    sources = {str(source['id']): source for source in payload.get('sources') or []}
    cited_source_ids = {str(sid) for sid in item.get('source_ids') or []}
    facts = []
    if cited_source_ids:
        rows = await (await conn.execute('''SELECT id,quote FROM app.career_material_facts
            WHERE goal_id=%s AND source_id=ANY(%s::uuid[])''',
            (goal_id, list(cited_source_ids)))).fetchall()
        facts = rows
    linked_facts = [{'id': str(fact['id']), 'quote': fact['quote']} for fact in facts
                    if any(str(fact['quote'] or '').strip() and
                           (str(fact['quote']).strip() in str(segments.get(sid, {}).get('text') or '')
                            or str(segments.get(sid, {}).get('text') or '').strip() == str(fact['quote']).strip())
                           for sid in item.get('evidence_segments') or [])]
    return {'kind': 'resume_bullet', 'draft_id': str(row['id']), 'version': row['version'],
            'index': index, 'text': item.get('text', ''), 'change_note': item.get('change_note', ''),
            'sources': [{k: source.get(k) for k in ('id', 'name', 'content_sha256')}
                        for sid in item.get('source_ids') or []
                        for source in [sources.get(str(sid), {})] if source],
            'segments': [{'id': sid, 'text': segments.get(sid, {}).get('text', ''),
                          'block': segments.get(sid, {}).get('block')}
                         for sid in item.get('evidence_segments') or []],
            'facts': linked_facts,
            'origin': (payload.get('authoring_record') or {}).get('origin') or row['origin'],
            'flagged': item.get('text', '')[:160] in flagged,
            'links': {'draft': str(row['id'])}}


async def observation(conn, goal_id, observation_id):
    row = await (await conn.execute('''SELECT o.*,c.reason AS correction,i.status AS session_status,i.input_payload
        FROM app.career_interview_observations o
        JOIN app.career_interviews i ON i.id=o.session_id
        LEFT JOIN app.career_interview_corrections c ON c.observation_id=o.id
        WHERE o.id=%s AND o.goal_id=%s''', (observation_id, goal_id))).fetchone()
    if not row:
        raise ApiError('resource_unavailable', 404)
    policy = (row['input_payload'] or {}).get('policy') or {}
    terminal = row['session_status'] not in ('PENDING', 'RUNNING', 'WAITING_FOR_USER', 'WAITING_CREDENTIALS')
    if not terminal and policy.get('mode') != 'coaching':
        # Same rule as the session view: feedback is not readable while an interview is live.
        raise ApiError('resource_unavailable', 404)
    payload = row['payload'] or {}
    resume = None
    draft_id = (row['input_payload'] or {}).get('draft_id')
    if draft_id:
        draft = await (await conn.execute('SELECT id,version FROM app.career_material_drafts WHERE id=%s AND goal_id=%s',
                                          (draft_id, goal_id))).fetchone()
        if draft:
            resume = {'draft_id': str(draft['id']), 'version': draft['version']}
    return {'kind': 'interview_observation', 'id': str(row['id']), 'session_id': str(row['session_id']),
            'answer_id': str(row['answer_id']), 'label': payload.get('label'),
            'summary': payload.get('summary'), 'answer_quote': payload.get('answer_quote'),
            'evidence_id': payload.get('evidence_id'), 'evidence_quote': payload.get('evidence_quote'),
            'correction': row['correction'], 'session_status': row['session_status'],
            'resume': resume,
            'links': {'session': str(row['session_id']), 'answer': str(row['answer_id'])}}


async def application_action(conn, goal_id, application_id):
    row = await (await conn.execute('''SELECT id,status,version,package_revision,created_at
        FROM app.career_applications WHERE id=%s AND goal_id=%s''', (application_id, goal_id))).fetchone()
    if not row:
        raise ApiError('resource_unavailable', 404)
    package = await (await conn.execute('''SELECT revision,payload FROM app.career_application_packages
        WHERE application_id=%s ORDER BY revision DESC LIMIT 1''', (application_id,))).fetchone()
    payload = (package or {}).get('payload') or {}
    refs = payload.get('evidence_refs') if isinstance(payload, dict) else None
    linked = [ref.split(':', 1)[1] for ref in (refs or []) if isinstance(ref, str) and ref.startswith('observation:')]
    # Sessions opened from this application bind it in their frozen input; their terminal
    # observations belong to it even though the package itself predates them.
    bound = await (await conn.execute('''SELECT o.id FROM app.career_interviews i
        JOIN app.career_interview_observations o ON o.session_id=i.id
        WHERE i.goal_id=%s AND i.input_payload#>>'{application,id}'=%s AND i.status='COMPLETED'
        ORDER BY o.created_at,o.id''', (goal_id, application_id))).fetchall()
    for row_observation in bound:
        identity = str(row_observation['id'])
        if identity not in linked:
            linked.append(identity)
    return {'kind': 'application_action', 'id': str(row['id']), 'status': row['status'],
            'version': row['version'], 'package_revision': row['package_revision'],
            'latest_package': (package or {}).get('revision'),
            'evidence_refs': refs or [], 'linked_observations': linked,
            'links': {'application': str(row['id'])}}
