"""Freeze selected resume text for interview questions, without creating confirmed facts."""
import hashlib
from uuid import UUID,uuid5
from interview_agent.storage.errors import ApiError


def resume_evidence(draft_id,version,payload):
    # F07Z-152: an uploaded resume is the user's own text. Once a position binds it, its lines are
    # usable as interview anchors, still marked as user-selected rather than confirmed facts.
    if payload.get('draft_style') not in ('rewrite','verbatim'):return []
    result=[]
    for index,item in enumerate(payload.get('rewrite_items') or []):
        source_ids=item.get('source_ids') or []
        if not source_ids:continue
        text=item['text'];sha=hashlib.sha256(text.encode()).hexdigest()
        result.append({'id':str(uuid5(UUID(draft_id),f'{index}:{sha}')),'kind':'project','quote':text[:240],
            'source_id':source_ids[0],'verification':'user_selected_resume_not_external_verified',
            'resume_source':{'draft_id':draft_id,'draft_version':version,'item_index':index,'text':text,
                             'content_sha256':sha,'source_ids':source_ids,'quote_truncated':len(text)>240}})
    return result


def select_resume_evidence(items,indexes):
    if indexes:
        by_index={f['resume_source']['item_index']:f for f in items}
        if not set(indexes)<=set(by_index):raise ApiError('resume_item_not_available',400)
        items=[by_index[i] for i in indexes]
    if len(items)>6:raise ApiError('resume_scope_too_large_select_items',400)
    return items
