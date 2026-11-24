"""F07Z-152 phase 3: a read-only directory of events across the goal's domains.

Only indexes (type, time, title, link ids) — no observation content, no merged narrative. Interview
observations stay behind the session endpoint and its terminal-visibility rule.
"""


async def timeline(conn, goal_id, limit=50):
    drafts = await (await conn.execute('''SELECT id,version,origin,created_at,payload
        FROM app.career_material_drafts WHERE goal_id=%s
        ORDER BY created_at DESC, id DESC LIMIT %s''', (goal_id, limit))).fetchall()
    bindings = await (await conn.execute('''SELECT b.id,b.created_at,b.active,b.resume_version,
            p.id AS position_id,s.payload->>'title' AS title,s.payload->>'company' AS company
        FROM app.career_position_bindings b
        JOIN app.career_positions p ON p.id=b.position_id
        LEFT JOIN app.career_job_snapshots s ON s.id=p.current_snapshot_id
        WHERE b.goal_id=%s ORDER BY b.created_at DESC, b.id DESC LIMIT %s''', (goal_id, limit))).fetchall()
    sessions = await (await conn.execute('''SELECT id,status,created_at,input_payload->'policy'->>'mode' AS mode
        FROM app.career_interviews WHERE goal_id=%s
        ORDER BY created_at DESC, id DESC LIMIT %s''', (goal_id, limit))).fetchall()
    applications = await (await conn.execute('''SELECT id,status,created_at
        FROM app.career_applications WHERE goal_id=%s
        ORDER BY created_at DESC, id DESC LIMIT %s''', (goal_id, limit))).fetchall()

    items = []
    for row in drafts:
        payload = row['payload'] or {}
        items.append({'type': 'resume_version', 'at': row['created_at'],
                      'title': ('原文导入' if payload.get('draft_style') == 'verbatim' else '简历')
                               + f' v{row["version"]}',
                      'origin': row['origin'], 'draft_id': str(row['id']),
                      'version': row['version'],
                      'item_count': len(payload.get('rewrite_items') or [])})
    for row in bindings:
        items.append({'type': 'position_binding', 'at': row['created_at'],
                      'title': f"{row['company'] or '岗位'} · {row['title'] or ''}".strip(' ·'),
                      'position_id': str(row['position_id']), 'resume_version': row['resume_version'],
                      'active': row['active'], 'binding_id': str(row['id'])})
    for row in sessions:
        items.append({'type': 'interview_session', 'at': row['created_at'],
                      'title': f"面试场次（{row['mode'] or 'interview'}）",
                      'session_id': str(row['id']), 'status': row['status']})
    for row in applications:
        items.append({'type': 'application', 'at': row['created_at'],
                      'title': '申请记录', 'application_id': str(row['id']), 'status': row['status']})
    items.sort(key=lambda item: (item['at'], item['type']), reverse=True)
    return {'items': items[:limit]}
