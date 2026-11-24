"""Deterministic claim guard for resume rewrites (S00G).

A cited segment says **where** a bullet came from; it does not prove the bullet is faithful to
it. The C2 review of `material_generation_probe` found 5/8 generated rewrites stayed inside the
citation but still exaggerated content, and the structural checks (quote resolves, source in
scope) rejected none of them.

This module is the product-side copy of the rule set that was measured as an evaluation artefact
(`evaluation/material_guard.py` now delegates here, so there is one implementation):

* no word blacklists: every rule compares the rewrite **against the source it cites**;
* families are separate so recall and false positives stay attributable;
* `activity` is deliberately **not** in `REVIEWED_FAMILIES`: on real output it flags legitimate
  paraphrase (重构→改造) as often as inflation, so it must not gate a draft;
* `number` compares numeric tokens: a number that is not in the source is a new claim, which is
  exactly the "30% from another project" failure.

Findings are data, not exceptions: the caller decides whether to revert the item to the source
wording, move it into suggestions, or (for a user's own edit) accept it.
"""
from typing import Literal

OUTCOME_VERBS = ('保障', '确保', '保证')
OUTCOME_OBJECTS = ('稳定运行', '稳定性', '可用性', '可靠性', '正常运行', '无损', '不出错')
FREQUENCY = ('日常', '定期', '持续', '经常', '长期', '每日', '每周', '实时')
RESPONSIBILITY = ('承接', '主导', '牵头', '独立完成', '整体推进', '全权')
WEAK_SOURCE = ('参与', '配合', '协助', '帮忙', '一部分', '其中')
ACTIVITY = ('设计与落地', '方案设计', '设计', '落地', '实施', '推进', '优化', '改造',
            '重构', '治理', '搭建', '选型', '升级', '上线', '部署', '压测')
# Source words that carry the user's stance. Dropping them turns "参与X" into "X", which reads
# as leading it — found in grounding-v3 and grounding-d output, invisible to the keyword list.
ROLE_MARKERS = ('参与', '配合', '协助', '帮忙', '一部分', '其中')

Family = Literal['outcome', 'frequency', 'responsibility', 'role_marker', 'activity', 'number']
ALL_FAMILIES: tuple[Family, ...] = ('outcome', 'frequency', 'responsibility', 'role_marker',
                                    'activity', 'number')
# The families that may gate a draft: measured precision, no known paraphrase collisions.
REVIEWED_FAMILIES: tuple[Family, ...] = ('outcome', 'frequency', 'responsibility', 'role_marker',
                                         'number')

CODE = {
    'outcome': 'unsupported_outcome',
    'frequency': 'unsupported_precision',
    'responsibility': 'responsibility_inflation',
    'role_marker': 'responsibility_inflation',
    'activity': 'unsupported_activity',
    'number': 'unsupported_number',
}


def _near(text, first, second, window=12):
    """True when `second` appears within `window` chars after `first`."""
    start = text.find(first)
    while start >= 0:
        if text.find(second, start, start + len(first) + window + len(second)) >= 0:
            return True
        start = text.find(first, start + 1)
    return False


def _numbers(text):
    """Numeric tokens with spaces and percent signs normalised away."""
    cleaned = (text.replace(' ', '').replace('　', '').replace('％', '%')
               .replace('，', ',').replace('。', '.'))
    tokens, current = set(), ''
    for char in cleaned:
        if char.isdigit() or (char == '.' and current and current[-1].isdigit()):
            current += char
        else:
            if current:
                tokens.add(current.rstrip('.'))
            current = ''
    if current:
        tokens.add(current.rstrip('.'))
    return tokens


def scan(text, source, families=ALL_FAMILIES):
    """Return explainable hits: [{family, code, term, why}]."""
    hits = []
    if not text:
        return hits

    if 'outcome' in families:
        for verb in OUTCOME_VERBS:
            if verb not in text or verb in source:
                continue
            for obj in OUTCOME_OBJECTS:
                if obj in text and obj not in source and _near(text, verb, obj):
                    hits.append({'family': 'outcome', 'code': CODE['outcome'],
                                 'term': verb + '…' + obj,
                                 'why': 'effect assertion absent from source'})
                    break

    if 'frequency' in families:
        for term in FREQUENCY:
            if term in text and term not in source:
                hits.append({'family': 'frequency', 'code': CODE['frequency'], 'term': term,
                             'why': 'regularity/frequency not stated in source'})
                break

    if 'responsibility' in families:
        if any(weak in source for weak in WEAK_SOURCE):
            for term in RESPONSIBILITY:
                if term in text and term not in source:
                    hits.append({'family': 'responsibility', 'code': CODE['responsibility'],
                                 'term': term,
                                 'why': 'stronger role than the source wording'})
                    break

    if 'role_marker' in families:
        if any(marker in source for marker in ROLE_MARKERS) and \
                not any(marker in text for marker in ROLE_MARKERS):
            hits.append({'family': 'role_marker', 'code': CODE['responsibility'],
                         'term': 'dropped role marker',
                         'why': 'source limited the stance (参与/配合/一部分) and the rewrite '
                                'states the activity without it'})

    if 'activity' in families:
        for term in ACTIVITY:
            if term in text and term not in source:
                hits.append({'family': 'activity', 'code': CODE['activity'], 'term': term,
                             'why': 'activity/scope absent from source'})
                break

    if 'number' in families:
        missing = sorted(_numbers(text) - _numbers(source))
        if missing:
            hits.append({'family': 'number', 'code': CODE['number'], 'term': missing[0],
                         'why': 'number/date/percentage absent from the cited source'})

    return hits


def codes(text, source, families=ALL_FAMILIES):
    return sorted({hit['code'] for hit in scan(text, source, families)})


def claims(text, source):
    """Only the reviewed families: what may block a draft or send an item to suggestions."""
    return scan(text, source, REVIEWED_FAMILIES)


def flagged_as_violation(text, source, families=ALL_FAMILIES):
    """One boolean per case, which is how the C2 verdict is expressed."""
    return bool(scan(text, source, families))

def ground_items(items, scope):
    """Split rewrite items into (kept, moved_texts, findings) against their cited source.

    Called on the *model-generated* draft, never on a user edit: the user may keep their own
    wording. An item whose claim its evidence does not support leaves the body and becomes a
    suggestion, so the owner sees what was withheld instead of losing it silently. If every item
    is flagged the draft would be empty, which is a failed proposal, not a silent blank resume.
    """
    from .segments import scope_segments

    sources = {source['id']: source['text'] for source in scope.get('sources') or []}
    segments = {segment['id']: segment['text'] for segment in scope_segments(scope)}
    kept, moved, findings = [], [], []
    for item in items:
        cited = '\n'.join(segments.get(segment_id, '') for segment_id in item.evidence_segments)
        source = cited.strip() or '\n'.join(sources.get(source_id, '')
                                           for source_id in item.source_ids)
        hits = claims(item.text, source) if source.strip() else []
        if not hits:
            kept.append(item)
            continue
        hit = hits[0]
        moved.append(item.text)
        findings.append({'code': hit['code'], 'family': hit['family'],
                         'term': str(hit['term'])[:60], 'text': item.text[:160],
                         'source_ids': list(item.source_ids),
                         'evidence_segments': list(item.evidence_segments)})
    if items and not kept:
        raise ValueError('claims_not_grounded')
    return kept, moved, findings
