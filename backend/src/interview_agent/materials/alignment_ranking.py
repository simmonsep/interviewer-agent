"""Scope-limited, deterministic ranking of confirmed facts for a JD (S00R).

The align branch may only use facts the owner already confirmed for this goal. This module does
not decide whether a fact supports a requirement (that is `alignment.validate_alignment` plus the
S00G claim rules); it only answers "which confirmed facts look related to this JD", so a long
fact list can be reduced to a bounded, explainable candidate set.

Properties:
- lexical and deterministic: CJK bigrams plus ASCII words, with a small CN/EN alias table; ties
  break by fact id, so the same input always yields the same order;
- no model, no database: the caller passes the already-authorized facts;
- an empty JD or no facts returns an empty ranking, never a random selection.
"""
from __future__ import annotations

import re

# A deliberately small alias table: only terms that actually show up in this product's JD text.
# It grows with evidence, not with imagination.
ALIASES = {
    '并发': ('concurrency', 'concurrent', 'thread', '线程'),
    '事务': ('transaction', 'acid'),
    '隔离': ('isolation',),
    '索引': ('index', 'btree'),
    '缓存': ('cache', 'redis'),
    '幂等': ('idempotent', 'idempotency'),
    '消息': ('message', 'queue', 'kafka', 'mq'),
    '锁': ('lock', 'mutex'),
    '性能': ('performance', 'latency', 'throughput', 'qps'),
    '测试': ('test', 'testing', 'unit test'),
    '接口': ('api', 'endpoint', 'rest'),
    '数据库': ('database', 'sql', 'postgres', 'mysql'),
    '分布式': ('distributed',),
}
_ASCII = re.compile(r'[a-z0-9+#.]{2,}')
_CJK = re.compile(r'[\u4e00-\u9fff]+')


def _norm(text):
    return (text or '').lower().replace('，', ',').replace('。', '.').replace('、', ',')


def terms(text):
    """CJK bigrams (and single chars for very short runs) plus ASCII words."""
    value = _norm(text)
    found = set(_ASCII.findall(value))
    for run in _CJK.findall(value):
        if len(run) == 1:
            found.add(run)
        else:
            found.update(run[index:index + 2] for index in range(len(run) - 1))
    for anchor, aliases in ALIASES.items():
        if anchor in value:
            found.update(aliases)
    return found


def rank(jd_text, facts, limit=6):
    """Return [{id, score, matched}] best-first; empty when there is nothing to rank."""
    wanted = terms(jd_text)
    if not wanted or not facts:
        return []
    scored = []
    for fact in facts:
        have = terms(fact.get('quote') or fact.get('text') or '')
        matched = sorted(wanted & have)
        if matched:
            scored.append({'id': fact['id'], 'score': len(matched), 'matched': matched[:8]})
    scored.sort(key=lambda row: (-row['score'], row['id']))
    return scored[:limit]
