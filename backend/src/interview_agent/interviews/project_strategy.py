"""新增业务 1 · 节点 B: project follow-up strategies, kept separate from scorable questions.

A strategy card is *how to probe*, not *what the candidate did*. It must never be presented as a
question with an answer, and it is only usable when the session has a real anchor (a frozen fact or
the candidate's own words). This module is pure: selection and verification only, no model call.

Falling back is the default. No matching card means the caller keeps the existing project template
instead of inventing a probe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from ..question_banks.ranking import keywords

Dimension = Literal['contribution', 'verification', 'failure', 'tradeoff']


@dataclass(frozen=True)
class StrategyCard:
    id: str
    title: str
    applies_to: tuple[str, ...] = ()          # technical tags, e.g. ('Redis', '锁')
    trigger: tuple[str, ...] = ()             # phrases in the candidate's own words
    dimensions: tuple[Dimension, ...] = ('contribution', 'verification', 'failure', 'tradeoff')
    example_question: str = ''
    source_url: str = ''
    source_note: str = ''
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {'id': self.id, 'title': self.title, 'applies_to': list(self.applies_to),
                'trigger': list(self.trigger), 'dimensions': list(self.dimensions),
                'example_question': self.example_question, 'source_url': self.source_url,
                'source_note': self.source_note}


def _overlap(text: str, phrases: Iterable[str]) -> int:
    haystack = keywords(text)
    return sum(1 for phrase in phrases if keywords(phrase) & haystack)


def choose_strategy(cards: Iterable[StrategyCard], *, anchor_text: str = '', jd_text: str = '',
                    dimension: Dimension, allow: bool = True) -> StrategyCard | None:
    """Best card for this anchor, or None to keep the existing template.

    `allow` is the session's `allow_project_questions`; a strategy may never re-enable project
    questions the user or the policy turned off.
    """
    if not allow:
        return None
    candidates = [card for card in cards if dimension in card.dimensions]
    if not candidates:
        return None
    scored = []
    for index, card in enumerate(candidates):
        score = 2 * _overlap(anchor_text, card.trigger) + _overlap(jd_text, card.applies_to)
        scored.append((score, index, card))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, _, best = scored[0]
    return best if best_score > 0 else None


def anchors_are_grounded(card: StrategyCard | None, *, fact_ids: Iterable[str],
                         answer_text: str = '') -> bool:
    """A card may only be used with an existing anchor.

    Either the session has a frozen material fact to attach to, or the candidate said something
    this round. With neither, the card would be a generic question dressed as a follow-up.
    """
    if card is None:
        return False
    return bool(list(fact_ids)) or bool((answer_text or '').strip())


# Small authored corpus, frozen into the session. Not user-editable yet: the plan asks for a
# structured fixture before adding storage and an import channel.
DEFAULT_STRATEGIES: dict[str, tuple[StrategyCard, ...]] = {
    'java-backend': (
        StrategyCard(id='lock-verification', title='锁的验证与失效边界',
                     applies_to=('Redis', '锁'), trigger=('SETNX', '过期时间', '锁'),
                     dimensions=('verification', 'failure'),
                     example_question='锁租期到了但业务还没跑完，新持有者接手后旧执行者会怎样？'),
        StrategyCard(id='tx-tradeoff', title='事务边界的取舍',
                     applies_to=('PostgreSQL', '事务'), trigger=('事务', '隔离', '回滚'),
                     dimensions=('tradeoff', 'contribution'),
                     example_question='这段事务为什么这样切边界？换一种切法会付出什么代价？'),
        StrategyCard(id='idempotency-failure', title='重试与重复提交',
                     applies_to=('接口', '幂等'), trigger=('重试', '幂等', '超时'),
                     dimensions=('failure', 'verification'),
                     example_question='请求超时后客户端重试，服务端怎样避免重复写入？你验证过吗？'),
    ),
    'ai-application': (
        StrategyCard(id='tool-contract', title='工具参数与权限边界',
                     applies_to=('工具调用', 'Agent'), trigger=('工具', '参数', '权限'),
                     dimensions=('verification', 'failure'),
                     example_question='模型给的参数格式合法但不属于当前用户时，服务端在哪一步拒绝？'),
        StrategyCard(id='memory-recovery', title='记忆与恢复的边界',
                     applies_to=('LangGraph', '记忆'), trigger=('checkpointer', 'thread_id', '恢复'),
                     dimensions=('contribution', 'verification'),
                     example_question='进程重启后要接着同一会话，哪些状态必须持久化，哪些可以重建？'),
        StrategyCard(id='retrieval-precision', title='检索命中的取舍',
                     applies_to=('RAG', '检索'), trigger=('召回', '检索', '向量'),
                     dimensions=('tradeoff', 'failure'),
                     example_question='召回结果全是关键词命中但跑题时，你怎样判断该不该用？'),
    ),
}


def cards_for(role: str) -> tuple[StrategyCard, ...]:
    return DEFAULT_STRATEGIES.get(role, ())
