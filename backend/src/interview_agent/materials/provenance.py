"""Per-item provenance for a draft (S00G): what each bullet cites, and how it was produced.

The panel should be able to answer, for one line of the resume: which source, which version,
which quoted segment, was it edited by the owner, and did the claim check flag it. Everything is
read from the stored draft payload, so the answer is the same later as it was at write time.
"""
from .segments import scope_segments


def item_provenance(payload):
    sources = {source['id']: source for source in payload.get('sources') or []}
    segments = {segment['id']: segment for segment in (payload.get('segments')
                                                       or scope_segments(payload))}
    findings = {}
    for finding in payload.get('grounding_findings') or []:
        findings.setdefault(finding.get('text'), finding)
    edited = payload.get('content_origin') == 'user_revision'
    rows = []
    for index, item in enumerate(payload.get('rewrite_items') or []):
        cited = [segments[segment_id] for segment_id in item.get('evidence_segments') or []
                 if segment_id in segments]
        rows.append({
            'index': index,
            'text': item.get('text', ''),
            'source_ids': list(item.get('source_ids') or []),
            'sources': [{'id': source_id,
                         'name': sources.get(source_id, {}).get('name'),
                         'content_sha256': sources.get(source_id, {}).get('content_sha256'),
                         'material_revision': sources.get(source_id, {}).get('material_revision')}
                        for source_id in item.get('source_ids') or []],
            'segments': [{'id': segment['id'], 'text': str(segment.get('text', ''))[:200],
                          'block': segment.get('block')} for segment in cited],
            'transformation': ('imported' if (payload.get('authoring_record') or {}).get('origin') == 'uploaded'
                               else 'user_edit' if edited else 'model_rewrite'),
            'confirmation': 'user_edited' if edited else 'model_draft_unconfirmed',
            'finding': findings.get(item.get('text')),
        })
    return rows
