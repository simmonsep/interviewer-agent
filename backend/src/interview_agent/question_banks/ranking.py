"""新增业务 1 · A3: deterministic retrieval ordering inside an already-frozen bank.

The rules are deliberately boring, because the alternative (a semantic model) costs money and
cannot be reviewed line by line:

1. filter: role, enabled bank, already-asked item ids, and items whose kind is not askable;
2. rank: lexical overlap of the JD / training direction / current topic with the item's title,
   tags, question and (never shown to the candidate) reference;
3. stable tie-break: the bank's own order, so equal scores never shuffle between runs.

No model call, no network, no mutation of the item. A miss simply leaves the caller with no new
candidate; the caller must not widen the scope on its own.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Chinese text has no spaces, so a keyword is "a run of ASCII terms or a CJK bigram".
_ASCII = re.compile(r'[a-zA-Z][a-zA-Z0-9+#._-]*')


def keywords(text: str) -> set[str]:
    lowered = (text or '').lower()
    terms = {token for token in _ASCII.findall(lowered) if len(token) >= 2}
    cjk = [ch for ch in lowered if '\u4e00' <= ch <= '\u9fff']
    terms.update(''.join(cjk[index:index + 2]) for index in range(len(cjk) - 1))
    return terms


def score(item: dict[str, Any], query: set[str], current_topic: str | None) -> int:
    """Higher is more relevant. Tags count double: they are the author's own labels."""
    if not query:
        return 0
    haystack = ' '.join(str(item.get(key) or '') for key in ('title', 'question', 'reference'))
    value = len(query & keywords(haystack))
    value += 2 * len(query & keywords(' '.join(item.get('tags') or [])))
    if current_topic and current_topic in (item.get('topic_ids') or []):
        value += 3
    return value


def rank(items: Iterable[dict[str, Any]], *, jd_text: str = '', direction: str = '',
         current_topic: str | None = None, asked: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Filter then order. Items keep their original fields; nothing is rewritten."""
    asked_ids = {str(item) for item in asked}
    query = keywords(jd_text) | keywords(direction)
    kept = [item for item in items
            if str(item.get('item_id')) not in asked_ids
            and item.get('kind', 'knowledge') in ('knowledge', 'short_answer')
            and (item.get('roles') is None or item.get('roles'))]
    ordered = sorted(enumerate(kept),
                     key=lambda pair: (-score(pair[1], query, current_topic), pair[0]))
    return [item for _, item in ordered]


def suggest_topics(topics: dict[str, dict], *, jd_text: str = '', direction: str = '',
                   limit: int = 5) -> list[dict[str, str]]:
    """Rank frozen topics for the model view.

    Only ids and titles are returned: the model can then `read_topics` the ones it wants. Sending
    the whole pack (references included) would blow the payload budget.
    """
    query = keywords(jd_text) | keywords(direction)
    scored = []
    for order, (topic_id, item) in enumerate((topics or {}).items()):
        score = score_topic(item, query)
        scored.append((score, order, topic_id, item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    picked = [row for row in scored if row[0] > 0][:limit] or scored[:limit]
    return [{'id': topic_id, 'title': str(item.get('title') or topic_id)} for _, _, topic_id, item in picked]


def score_topic(item: dict, query: set[str]) -> int:
    if not query:
        return 0
    haystack = ' '.join(str(item.get(key) or '') for key in ('title', 'base', 'reference'))
    return len(query & keywords(haystack)) + 2 * len(query & keywords(' '.join(item.get('tags') or [])))


# Bigrams that appear in almost every JD and say nothing about the teaching point.
GENERIC = frozenset({
    '熟悉', '了解', '掌握', '应用', '实习', '开发', '设计', '优化', '实现', '问题', '什么',
    '怎样', '如何', '可以', '需要', '说明', '验证', '定位', '方案', '使用', '进行', '相关',
    '工作', '经验', '能力', '要求', '负责', '参与', '一个', '我们', '自己', '以及', '或者',
})


def preferred_private_ids(topics: dict[str, dict], *, jd_text: str = '', direction: str = '',
                          limit: int = 5) -> list[str]:
    """Private-bank topic ids that match this JD/direction, best first.

    Source is read from `bank_item.origin` (a selected built-in bank can also produce `b-` ids).
    Only the *teaching point* counts - title and base question, minus generic bigrams. A shared
    word in the reference, the URL or a coarse tag is not enough: live rag16 asked a cache-hit-rate
    question for a locks/idempotency JD because both mention Redis (and even an explicit
    "fall back when the point differs" line did not change the choice). No positive match means no
    candidate, which keeps the role pack in play instead of pushing an off-topic bank item.
    """
    query = keywords(jd_text) | keywords(direction)
    if not query:
        return []
    matched = []
    for order, (topic_id, item) in enumerate((topics or {}).items()):
        origin = str((item.get('bank_item') or {}).get('origin') or '')
        if origin != 'private':
            continue
        teaching = f"{item.get('title') or ''} {item.get('base') or ''}"
        score = len(query & (keywords(teaching) - GENERIC))
        if score > 0:
            matched.append((score, order, topic_id))
    matched.sort(key=lambda row: (-row[0], row[1]))
    return [topic_id for _, _, topic_id in matched[:limit]]
