"""F07Z-167 Phase 2: long-round context, early-answer anchors and candidate capacity.

Ten rounds must still be able to point back at round one by id after the recent window moved on;
the model reads that original answer through `read_answer`, never through a summary. Capacity is
counted from authored base/probe questions plus the four project focus dimensions, so a full
session only requires ten rounds when its frozen candidates can actually support them.
"""
import asyncio
from uuid import uuid4

import pytest

from interview_agent.interviews.agent import interview
from interview_agent.interviews.catalog import pack, topic
from interview_agent.interviews.context import compile_interview_context, read_answer
from interview_agent.interviews.contracts import Decision
from interview_agent.interviews.simulation import capacity, minimum_questions
from interview_agent.runtime.types import AssistantResponse, ToolCall
from test_interview_agent import observation, scope, scripted


def long_scope(rounds=10):
    questions = []
    answers = []
    for index in range(rounds):
        identity = str(uuid4())
        question = {'id': identity, 'topic_id': 'java-concurrency',
                    'question_id': f'java-concurrency-base-{index}', 'kind': 'base',
                    'text': f'第{index + 1}轮问题', 'assisted': False, 'fact_id': None}
        questions.append(question)
        answers.append({'id': str(uuid4()), 'question_id': identity,
                        'topic_id': 'java-concurrency', 'assisted': False,
                        'text': f'第{index + 1}轮原始回答：' + '细节' * 300})
    return {'policy': {'role': 'java-backend', 'mode': 'interview', 'version': 'ja05-policy-v1',
                       'max_questions': 12, 'profile': 'full', 'minimum_questions': 8},
            'pack': pack('java-backend'), 'target': {'kind': 'direction'},
            'facts': [], 'prior_observations': [], 'assisted_topics': [],
            'question_history': questions, 'current_question': questions[-1],
            'answer': {'id': answers[-1]['id'], 'text': answers[-1]['text'], 'assisted': False},
            'answers': answers, 'skipped_question_ids': [questions[2]['id']],
            'remaining_seconds': 900}


def test_early_anchors_keep_rounds_one_to_three_after_ten_rounds():
    s = long_scope()
    view = compile_interview_context(s)
    assert [anchor['answer_id'] for anchor in view['early_anchors']] == [
        item['id'] for item in s['answers'][:3]]
    assert [anchor['question_id'] for anchor in view['early_anchors']] == [
        question['id'] for question in s['question_history'][:3]]
    # An excerpt of the user's own answer, not a model summary; long text is marked truncated.
    assert view['early_anchors'][0]['text'].startswith('第1轮原始回答：')
    assert view['early_anchors'][0]['text_truncated'] is True
    # The recent window still trims the middle; the ids remain the way back.
    assert len(view['question_history']) == 6
    assert view['working_state']['question_count'] == 10
    assert view['working_state']['skipped_question_ids'] == [s['question_history'][2]['id']]
    assert view['working_state']['profile'] == 'full'
    assert view['working_state']['remaining_seconds'] == 900
    # The long-round view stays bounded for the 32 KiB request budget (F07Z-93): only six recent
    # questions plus three anchors survive whatever the session length.
    import json
    assert len(json.dumps(view, ensure_ascii=False).encode()) < 30_000


def test_read_answer_is_bounded_and_only_reads_this_session():
    s = long_scope(3)
    early = s['answers'][0]
    value = read_answer(s, early['id'])
    assert value['answer_id'] == early['id'] and value['question_id'] == early['question_id']
    assert value['text'].startswith('第1轮原始回答：') and value['truncated'] is True
    with pytest.raises(KeyError):
        read_answer(s, str(uuid4()))


def test_the_agent_can_read_an_early_answer_by_id():
    s = scope(answer='第一轮原始回答：我用FastAPI做过幂等接口。')
    s['answers'] = [{'id': s['answer']['id'], 'question_id': s['current_question']['id'],
                     'topic_id': 'java-concurrency', 'text': s['answer']['text'],
                     'assisted': False}]
    decision = Decision(action='ask_question', topic_id='java-concurrency',
                        question_id='java-concurrency-probe', observation=observation(s))
    captured = []

    class Reader:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            captured.extend(messages)
            name, args = [('read_interview_scope', {}),
                          ('read_answer', {'answer_id': s['answer']['id']}),
                          ('read_topics', {'topic_ids': ['java-concurrency']}),
                          ('submit_interview_action', decision.model_dump())][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Reader(), asyncio.Event()))
    assert value.action == 'ask_question'
    assert any(item['tool'] == 'read_answer' and item['status'] == 'ok' for item in value.tool_log)
    assert any('第一轮原始回答' in getattr(message, 'text', '') for message in captured)


def test_an_unknown_answer_id_is_refused_without_echoing_it():
    s = scope(answer='一次回答。')
    s['answers'] = [{'id': s['answer']['id'], 'question_id': s['current_question']['id'],
                     'topic_id': 'java-concurrency', 'text': s['answer']['text'],
                     'assisted': False}]
    decision = Decision(action='ask_question', topic_id='java-concurrency',
                        question_id='java-concurrency-probe', observation=observation(s))

    class Reader:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            name, args = [('read_interview_scope', {}),
                          ('read_answer', {'answer_id': 'sk-other-session'}),
                          ('read_topics', {'topic_ids': ['java-concurrency']}),
                          ('submit_interview_action', decision.model_dump())][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Reader(), asyncio.Event()))
    failed = next(item for item in value.tool_log if item['tool'] == 'read_answer')
    assert failed['status'] == 'error' and failed['error_code'] == 'answer_not_in_session'


def test_scripted_full_profile_walks_project_focuses_before_topics():
    s = scope()
    s['policy']['profile'] = 'full'
    s['policy']['max_questions'] = 12
    fact = str(uuid4())
    s['facts'] = [{'id': fact, 'quote': '我参与了团队的Redis通知模块。'}]
    opening = scripted(s)
    assert opening.topic_id == 'project' and opening.project_focus == 'contribution'
    question = {'id': str(uuid4()), 'topic_id': 'project', 'question_id': 'project-contribution',
                'kind': 'project', 'fact_id': fact, 'project_focus': 'contribution',
                'text': '请区分你亲自完成的部分。', 'assisted': False}
    s.update(current_question=question, question_history=[question],
             answer={'id': str(uuid4()), 'text': '我负责缓存与通知重试。', 'assisted': False})
    followup = scripted(s)
    assert followup.topic_id == 'project' and followup.project_focus == 'verification'


def test_a_rejected_citation_gets_a_repair_and_can_resubmit():
    """F07Z-167 live finding: a bad citation must be repairable in the next round, not by reading."""
    s = scope(answer='我不清楚。')
    decision = Decision(action='ask_question', topic_id='java-concurrency',
                        question_id='java-concurrency-probe', observation=observation(s))
    bad = decision.model_dump()
    bad['observation']['evidence_quote'] = '凭空补造的标准答案'
    seen = []

    class Repair:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            seen.extend(messages)
            name, args = [('read_interview_scope', {}),
                          ('read_topics', {'topic_ids': ['java-concurrency']}),
                          ('submit_interview_action', bad),
                          ('submit_interview_action', decision.model_dump())][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Repair(), asyncio.Event()))
    assert value.action == 'ask_question'
    failed = next(item for item in value.tool_log if item['status'] == 'error')
    assert failed['error_code'] == 'reference_citation_required'
    assert any('"repair"' in getattr(message, 'text', '') for message in seen)


def test_a_fifth_round_can_repair_a_rejected_submission():
    """F07Z-167: five requests per round allow scope + read + two rejections + one repair."""
    s = scope()
    s['question_history'] = [{'id': str(uuid4()), 'topic_id': 'java-concurrency',
                              'question_id': 'java-concurrency-base', 'kind': 'base',
                              'text': '已问过的问题', 'assisted': False}]
    bad = Decision(action='ask_question', topic_id='java-concurrency',
                   question_id='java-concurrency-base').model_dump()
    good = Decision(action='ask_question', topic_id='sql-isolation',
                    question_id='sql-isolation-base').model_dump()

    class Five:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            name, args = [('read_interview_scope', {}),
                          ('read_topics', {'topic_ids': ['java-concurrency', 'sql-isolation']}),
                          ('submit_interview_action', bad),
                          ('submit_interview_action', bad),
                          ('submit_interview_action', good)][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Five(), asyncio.Event()))
    assert value.topic_id == 'sql-isolation'
    assert len([item for item in value.tool_log if item['status'] == 'error']) == 2


def test_a_schema_error_lists_allowed_fields_without_echoing_the_unknown_key():
    s = scope()
    good = Decision(action='ask_question', topic_id='java-concurrency',
                    question_id='java-concurrency-base').model_dump()
    bad = dict(good, **{'SYNTHETIC-SECRET-FIELD': 'SYNTHETIC-SECRET-VALUE'})
    seen = []

    class Repair:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            seen.extend(messages)
            name, args = [('read_interview_scope', {}),
                          ('read_topics', {'topic_ids': ['java-concurrency']}),
                          ('submit_interview_action', bad),
                          ('submit_interview_action', good)][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Repair(), asyncio.Event()))
    assert value.action == 'ask_question'
    failed = next(item for item in value.tool_log if item['status'] == 'error')
    assert failed['error_code'] == 'invalid_interview_decision_schema'
    repair_messages = [m for m in seen if 'allowed_fields' in getattr(m, 'text', '')]
    assert repair_messages and 'observation' in getattr(repair_messages[0], 'text', '')
    assert all('SYNTHETIC-SECRET' not in getattr(m, 'text', '') for m in seen)


def test_an_already_asked_question_lists_the_unasked_ids_for_repair():
    """F07Z-167 matrix finding: without the list, two guesses can use the whole four-round budget."""
    s = scope()
    s['question_history'] = [{'id': str(uuid4()), 'topic_id': 'java-concurrency',
                              'question_id': 'java-concurrency-base', 'kind': 'base',
                              'text': '已问过的问题', 'assisted': False}]
    bad = Decision(action='ask_question', topic_id='java-concurrency',
                   question_id='java-concurrency-base').model_dump()
    good = Decision(action='ask_question', topic_id='sql-isolation',
                    question_id='sql-isolation-base').model_dump()
    seen = []

    class Repair:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            seen.extend(messages)
            name, args = [('read_interview_scope', {}),
                          ('read_topics', {'topic_ids': ['java-concurrency', 'sql-isolation']}),
                          ('submit_interview_action', bad),
                          ('submit_interview_action', good)][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Repair(), asyncio.Event()))
    assert value.topic_id == 'sql-isolation'
    failed = next(item for item in value.tool_log if item['status'] == 'error')
    assert failed['error_code'] == 'question_already_asked'
    assert any('unasked_questions' in getattr(message, 'text', '')
               and 'sql-isolation-base' in getattr(message, 'text', '') for message in seen)
    repair_messages = [message for message in seen
                       if 'unasked_questions' in getattr(message, 'text', '')]
    assert repair_messages
    # Without a current answer a probe is not available, so it must not be offered as a repair.
    assert all('sql-isolation-probe' not in getattr(message, 'text', '')
               for message in repair_messages)


def test_a_project_question_without_facts_returns_a_repair():
    s = scope()
    bad = Decision(action='ask_question', topic_id='project',
                   question_id='project-contribution').model_dump()
    good = Decision(action='ask_question', topic_id='sql-isolation',
                    question_id='sql-isolation-base').model_dump()
    seen = []

    class Repair:
        n = 0

        async def complete(self, messages, tools):
            self.n += 1
            seen.extend(messages)
            name, args = [('read_interview_scope', {}),
                          ('read_topics', {'topic_ids': ['sql-isolation']}),
                          ('submit_interview_action', bad),
                          ('submit_interview_action', good)][self.n - 1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    value = asyncio.run(interview(s, Repair(), asyncio.Event()))
    assert value.topic_id == 'sql-isolation'
    failed = next(item for item in value.tool_log if item['status'] == 'error')
    assert failed['error_code'] == 'project_question_not_enabled'
    assert any('"project_questions":false' in getattr(message, 'text', '').replace(' ', '')
               for message in seen)


def test_capacity_counts_authored_questions_and_project_focuses():
    role = pack('java-backend')
    assert capacity(role['topic_order'], role['topics'], [], False) == 8
    assert capacity(role['topic_order'], role['topics'], [{'id': 'f1'}], True) == 12
    assert minimum_questions('full', 8) == 8
    assert minimum_questions('full', 12) == 10
    # Authored retest questions exist only in coaching, so they must not inflate the promise.
    assert capacity(['practice'], {'practice': {'base': '题'}}, [], False) == 1
