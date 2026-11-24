"""Per-version authoring record: what this version changed and which sources it cites.

The structure is computed from stored data; the model only contributes each item's change_note.
"""


def build(payload: dict, *, origin: str, version: int, parent_version: int | None) -> dict:
    sources = [{k: source.get(k) for k in ('id', 'name', 'content_sha256')}
               for source in payload.get('sources') or []]
    items = []
    for index, item in enumerate(payload.get('rewrite_items') or []):
        items.append({'index': index, 'text': item.get('text', ''),
                      'source_ids': list(item.get('source_ids') or []),
                      'evidence_segments': list(item.get('evidence_segments') or []),
                      'change_note': item.get('change_note', '')})
    return {'origin': origin, 'version': version, 'parent_version': parent_version,
            'sources': sources, 'items': items}
