"""S06B: offline replay pairs for "no progress", measured without a model call.

The question is narrow: can a conservative early-stop rule remove model calls on provably
unproductive rounds while leaving every legal sample successful? The samples are frozen scripted
sequences through the real graph (same tools, same validation, same four-round ceiling).
"""
import asyncio

import pytest

from interview_agent.interviews.agent import InterviewFailure, interview
from interview_agent.interviews.progress import summarize, would_stop_early
from interview_agent.runtime.types import AssistantResponse, ToolCall
from test_core_quality import mixed, project
from test_interview_diagnostics import InvalidSubmission, failed


class RepeatedReads:
    """Round 1 reads the scope, then every later round re-reads the same topic."""

    def __init__(self, topic_id):
        self.topic_id, self.n = topic_id, 0

    async def complete(self, messages, tools):
        self.n += 1
        if self.n == 1:
            name, args = 'read_interview_scope', {}
        else:
            name, args = 'read_topics', {'topic_ids': [self.topic_id]}
        return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])


def test_repeated_identical_reads_are_visible_in_the_diagnostic():
    scope = mixed()
    topic_id = scope['pack']['topic_order'][0]
    provider = RepeatedReads(topic_id)
    with pytest.raises(InterviewFailure, match='interview_model_limit') as caught:
        asyncio.run(interview(scope, provider, asyncio.Event()))
    progress = caught.value.diagnostic['progress']
    assert progress['rounds'] == 4
    assert progress['read_rounds'] == 3
    assert progress['new_evidence_rounds'] == 1          # only the first read was new
    assert progress['repeated_read_rounds'] == 2        # rounds 3 and 4 repeated round 2
    assert would_stop_early(progress) is True


def test_the_rule_cannot_save_a_call_inside_the_four_round_ceiling():
    """The rule needs two repeats; with the scope read first, those land on round four."""
    scope = mixed()
    topic_id = scope['pack']['topic_order'][0]
    provider = RepeatedReads(topic_id)
    with pytest.raises(InterviewFailure) as caught:
        asyncio.run(interview(scope, provider, asyncio.Event()))
    trace = caught.value.diagnostic['tool_log']
    repeat_rounds = [entry['round'] for entry in trace
                     if entry.get('tool') == 'read_topics' and entry.get('new_topics') == 0]
    stop_round = repeat_rounds[1] if len(repeat_rounds) > 1 else None
    assert stop_round == 4
    assert max(0, 4 - stop_round) == 0


def test_the_legal_fourth_round_correction_is_not_no_progress():
    scope = mixed()
    payload = project(scope).model_dump()
    payload['question_text'] = 'SYNTHETIC-PRIVATE-TEXT'   # rejected, then never reused
    provider = InvalidSubmission(payload)
    with pytest.raises(InterviewFailure) as caught:
        asyncio.run(interview(scope, provider, asyncio.Event()))
    progress = caught.value.diagnostic['progress']
    assert progress['candidate_produced'] is False
    assert progress['identical_rejected_submissions'] == 1   # two submissions, both rejected
    assert would_stop_early(progress) is False


def test_a_successful_correction_reports_a_produced_candidate():
    """The bank sample corrects on the round after one rejection: it must never look stalled."""
    from test_bank_decision_repair import BankRepair, bank_scope
    scope, tid = bank_scope()
    provider = BankRepair(scope, tid, {'question_text': 'SYNTHETIC-PRIVATE-CONTENT'}, True)
    decision = asyncio.run(interview(scope, provider, asyncio.Event()))
    progress = summarize(decision.tool_log)
    assert progress['candidate_produced'] is True
    assert progress['identical_rejected_submissions'] == 0
    assert would_stop_early(progress) is False
