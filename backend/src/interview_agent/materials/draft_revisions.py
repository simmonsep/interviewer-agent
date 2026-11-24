"""Owner-driven resume revisions; no model request or fact promotion."""
from uuid import UUID
from psycopg.types.json import Jsonb
from interview_agent.storage.errors import ApiError
from interview_agent.career.repository import digest
from .claim_guard import claims
from .contracts import MaterialResult,render_draft
from .segments import scope_segments


def edit_findings(items,scope):
    """S00Gc: record, never rewrite. The owner may keep their own wording; what we add is the
    machine-readable answer to "does the segment it cites still support this sentence?"."""
    sources={source['id']:source['text'] for source in scope.get('sources') or []}
    segments={segment['id']:segment['text'] for segment in scope_segments(scope)}
    findings=[]
    for item in items:
        cited='\n'.join(segments.get(segment_id,'') for segment_id in item.evidence_segments)
        source=cited.strip() or '\n'.join(sources.get(source_id,'') for source_id in item.source_ids)
        hits=claims(item.text,source) if source.strip() else []
        if not hits:continue
        hit=hits[0]
        findings.append({'code':hit['code'],'family':hit['family'],'term':str(hit['term'])[:60],
                         'text':item.text[:160],'source_ids':list(item.source_ids),
                         'evidence_segments':list(item.evidence_segments),'origin':'user_edit'})
    return findings[:16]


async def revise_draft(repo,principal,goal_id,draft_id,key,value):
    draft_id=str(UUID(draft_id))
    async with repo.pool.connection() as conn,conn.transaction():
        # Same goal lock as model completion serializes version allocation.
        await repo._user(conn,principal,goal_id)
        request_hash=digest({**value.model_dump(mode='json'),'draft_id':draft_id})
        prior=await (await conn.execute('SELECT id,request_hash FROM app.career_material_drafts WHERE goal_id=%s AND request_id=%s',(goal_id,key))).fetchone()
        if prior:
            if prior['request_hash']!=request_hash:raise ApiError('idempotency_conflict',409)
            return str(prior['id']),False
        goal,state=await repo._user(conn,principal,goal_id,value)
        base=await (await conn.execute('SELECT * FROM app.career_material_drafts WHERE goal_id=%s AND id=%s',(goal_id,draft_id))).fetchone()
        if not base:raise ApiError('resource_unavailable',404)
        payload=base['payload']
        if payload.get('draft_style')!='rewrite':raise ApiError('rewrite_draft_required',400)
        if (base['goal_version']!=goal['version'] or base['material_revision']!=state['revision']
                or digest(payload['target'])!=digest(await repo._target(conn,goal))):
            raise ApiError('material_draft_stale',409)
        latest=await (await conn.execute('SELECT max(version) AS version FROM app.career_material_drafts WHERE goal_id=%s',(goal_id,))).fetchone()
        if latest['version']!=value.expected_draft_version:raise ApiError('draft_version_conflict',409)
        if latest['version']>=100:raise ApiError('draft_version_limit',409)
        if value.action!='restore' and base['version']!=latest['version']:raise ApiError('draft_version_conflict',409)
        if (value.action=='edit')!=bool(value.items):raise ApiError('invalid_revision_items',400)
        if value.action!='edit' and value.selected_suggestion_indexes:raise ApiError('suggestions_require_edit',400)
        sections={key:(getattr(value,key) if getattr(value,key) is not None else payload.get(key,[]))
                  for key in ('basics','education','experience','projects','skills')}
        if value.action!='edit' and any(getattr(value,key) is not None for key in sections):
            raise ApiError('sections_require_edit',400)
        suggestions=payload.get('suggestions',[])
        if any(i>=len(suggestions) for i in value.selected_suggestion_indexes):raise ApiError('suggestion_not_available',400)
        scope={'mode':'draft','draft_style':'rewrite','target':payload['target'],
               'sources':payload['sources'],'segments':payload.get('segments',[])}
        items=value.items if value.action=='edit' else payload['rewrite_items']
        try:
            markdown,new_payload=render_draft(scope,MaterialResult(action='draft',rewrite_items=items,
                                                                   basics=sections['basics'],
                                                                   education=sections['education'],
                                                                   experience=sections['experience'],
                                                                   projects=sections['projects'],
                                                                   skills=sections['skills'],
                                                                   omitted_segments=payload.get('omitted_segments',[]),
                                                                   truncated_segment_ids=payload.get('truncated_segment_ids',[]),
                                                                   structured_segment_ids=payload.get('structured_segment_ids',[]),
                                                                   structured_sections=payload.get('structured_map',{}),
                                                                   suggestions=suggestions))
        except ValueError:raise ApiError('invalid_revision_items',400) from None
        selected=value.selected_suggestion_indexes if value.action=='edit' else payload.get('selected_suggestion_indexes',[])
        new_payload.update(candidate_status='user_selected' if value.action=='accept' else 'user_edited' if value.action=='edit' else 'user_restored',
            revision_action=value.action,parent_draft_id=draft_id,
            selected_suggestion_indexes=selected,
            selected_suggestions=[suggestions[i] for i in selected],
            content_origin='user_revision',change_note='由用户选择或编辑；保留历史版本，不自动新增确认经历。')
        if value.action=='edit':
            # Nothing is removed or rewritten here (the user's words win); the finding tells the
            # panel that the cited evidence no longer supports the sentence it was edited into.
            new_payload['grounding_findings']=edit_findings(items,scope)
        from .authoring import build as authoring_record
        new_payload['authoring_record']=authoring_record(new_payload,origin='user_edit',
            version=latest['version']+1,parent_version=base['version'])
        # A later revision can still cite any original source retained by its parent.
        new_payload['sources']=payload['sources']
        row=await (await conn.execute('''INSERT INTO app.career_material_drafts
            (goal_id,version,goal_version,material_revision,payload,markdown,parent_draft_id,request_id,request_hash,origin)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'user_edit') RETURNING id''',
            (goal_id,latest['version']+1,goal['version'],state['revision'],Jsonb(new_payload),markdown,draft_id,key,request_hash))).fetchone()
        return str(row['id']),True
