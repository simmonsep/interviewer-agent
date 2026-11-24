"""The one rule for 'what may leave the product as resume text'.

Download, CLI export and a new application package all read this function. An item leaves when it
has a source/segment, is not flagged by the claim guard, and is not explicitly unconfirmed.
"""


def flagged_texts(payload: dict) -> set[str]:
    return {str(finding.get('text') or '') for finding in payload.get('grounding_findings') or []}


def usable_items(payload: dict) -> list[dict]:
    flagged = flagged_texts(payload)
    items = []
    for item in payload.get('rewrite_items') or []:
        text = str(item.get('text') or '')
        if not text.strip():
            continue
        if not (item.get('source_ids') or item.get('evidence_segments')):
            continue
        if text[:160] in flagged:
            continue
        items.append(item)
    return items


def usable_markdown(payload: dict, fallback: str = '') -> str:
    items = usable_items(payload)
    if not items:
        return fallback if not payload.get('rewrite_items') else ''
    return '\n\n'.join(f'- {item["text"]}' for item in items) + '\n'
