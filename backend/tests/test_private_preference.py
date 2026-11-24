"""新增业务 1 §2.1: a matching private candidate comes first, and a fallback needs a reason."""
import asyncio
import json

import pytest

from interview_agent.interviews.agent import interview
from interview_agent.interviews.catalog import pack
from interview_agent.interviews.contracts import Decision, question_payload, validate_decision
from interview_agent.runtime.types import AssistantResponse, ToolCall

PRIVATE_ID = 'b-0123456789abcdef'


def scope(jd: str = '熟悉 LangGraph 的状态恢复与 checkpointer'):
    authored = pack('ai-application')
    source_id = authored['topic_order'][0]
    clone = {**authored['topics'][source_id],
             'title': 'LangGraph 的状态与恢复',
             'base': 'state、checkpointer 与 thread_id 各自负责什么？',
             'has_reference': True,
             'bank_item': {'origin': 'private', 'item_id': source_id, 'bank_id': 'b-1',
                           'version': 1, 'revision': 1, 'name': 'user-bank', 'enabled': True,
                           'item_count': 1}}
    topics = {**authored['topics'], PRIVATE_ID: clone}
    return {
        'pack': {**authored, 'topics': topics,
                 'topic_order': [PRIVATE_ID, *authored['topic_order']]},
        'policy': {'role': 'ai-application', 'mode': 'interview', 'max_questions': 4,
                   'version': 'ja05-policy-v1', 'allow_project_questions': False},
        'facts': [], 'answer': None, 'current_question': None, 'question_history': [],
        'training_context': {'jd_text': jd, 'direction': 'AI 应用'},
        'strategies': [], 'assisted_topics': [],
    }


def ask(topic_id: str, question_id: str | None = None, **overrides):
    payload = {'action': 'ask_question', 'topic_id': topic_id,
               'question_id': question_id or topic_id + '-base'}
    payload.update(overrides)
    return Decision.model_validate(payload)


def role_topic(s):
    return next(tid for tid in s['pack']['topic_order'] if tid != PRIVATE_ID)


def test_the_first_technical_question_takes_the_matching_private_candidate():
    validate_decision(scope(), ask(PRIVATE_ID))


def test_choosing_a_role_question_without_a_reason_is_rejected():
    s = scope()
    with pytest.raises(ValueError, match='private_candidate_preferred'):
        validate_decision(s, ask(role_topic(s)))


def test_a_grounded_fallback_needs_both_reason_and_candidate_reference():
    s = scope()
    with pytest.raises(ValueError, match='private_candidate_preferred'):
        validate_decision(s, ask(role_topic(s), fallback_reason='考点不同'))
    validate_decision(s, ask(role_topic(s), fallback_reason='私有候选考的是状态恢复，当前考点是检索召回',
                             fallback_candidate_id=PRIVATE_ID))


def test_an_unrelated_jd_does_not_constrain_the_role_pack():
    s = scope(jd='多模态视觉：CUDA 算子优化与扩散模型训练')
    validate_decision(s, ask(role_topic(s)))


def test_the_question_event_records_the_source_and_the_fallback_reason():
    s = scope()
    private = validate_decision(s, ask(PRIVATE_ID))
    assert question_payload(s, private)['source'] == 'private_bank'
    role = role_topic(s)
    fallback = validate_decision(s, ask(role, fallback_reason='私有候选考的是状态恢复，当前考点是检索召回',
                                        fallback_candidate_id=PRIVATE_ID))
    payload = question_payload(s, fallback)
    assert payload['source'] == 'role_pack'
    assert payload['fallback_reason'] == '私有候选考的是状态恢复，当前考点是检索召回'


def test_only_the_first_technical_question_is_constrained():
    s = scope()
    role = role_topic(s)
    # A technical base question was already asked, so later choices are free again.
    s['question_history'] = [{'topic_id': PRIVATE_ID, 'kind': 'base',
                              'question_id': PRIVATE_ID + '-base', 'text': 'x'}]
    validate_decision(s, ask(role))


def test_a_project_question_first_leaves_the_first_technical_one_constrained():
    # Plan §2.1: the project question keeps its own rules; the first *technical* question after it
    # still prefers the matching private candidate.
    s = scope()
    s['question_history'] = [{'topic_id': 'project', 'kind': 'project',
                              'question_id': 'project-contribution', 'text': 'x'}]
    with pytest.raises(ValueError, match='private_candidate_preferred'):
        validate_decision(s, ask(role_topic(s)))
    validate_decision(s, ask(PRIVATE_ID))


def test_a_builtin_item_with_a_b_prefixed_id_is_not_a_private_candidate():
    # A selected built-in bank also produces b- ids, so the source is read from bank_item.origin.
    s = scope()
    s['pack']['topics'][PRIVATE_ID]['bank_item']['origin'] = 'builtin'
    validate_decision(s, ask(role_topic(s)))


def test_a_private_item_without_a_reference_stays_a_candidate():
    # The user imported it deliberately; the view flags has_reference instead of dropping it.
    s = scope()
    s['pack']['topics'][PRIVATE_ID]['has_reference'] = False
    validate_decision(s, ask(PRIVATE_ID))


def test_an_old_session_without_training_context_is_unconstrained():
    s = scope()
    s.pop('training_context')
    validate_decision(s, ask(role_topic(s)))


def test_an_enabled_practice_mistake_still_owns_the_opening_question():
    s = scope()
    practice_id = 'practice-cache-hit'
    s['pack']['topics'][practice_id] = {**s['pack']['topics'][role_topic(s)],
                                        'title': '缓存命中率复盘',
                                        'origin': 'practice_record'}
    s['pack']['topic_order'] = [practice_id, *s['pack']['topic_order']]
    validate_decision(s, ask(practice_id))


def test_a_rejected_role_choice_teaches_the_preferred_ids_and_fallback_fields():
    # A live run lost both remaining rounds to a bare 'invalid_interview_tool'; the feedback now
    # carries the ids and the two fields, so one retry is enough.
    from test_interview_agent import scope as agent_scope

    s = agent_scope()
    s['training_context'] = {'jd_text': '熟悉 LangGraph 的状态恢复与 checkpointer', 'direction': 'AI 应用'}
    s['strategies'] = []
    authored = s['pack']
    source_id = authored['topic_order'][0]
    private = {**authored['topics'][source_id],
               'title': 'LangGraph 的状态与恢复',
               'base': 'state、checkpointer 与 thread_id 各自负责什么？',
               'has_reference': True,
               'bank_item': {'origin': 'private', 'item_id': source_id, 'bank_id': 'b-1',
                             'version': 1, 'revision': 1, 'name': 'user-bank', 'enabled': True,
                             'item_count': 1}}
    s['pack'] = {**authored, 'topics': {**authored['topics'], PRIVATE_ID: private},
                 'topic_order': [PRIVATE_ID, *authored['topic_order']]}
    role = authored['topic_order'][1]

    class Reader:
        n = 0
        feedback = None

        async def complete(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return AssistantResponse(tool_calls=[ToolCall(id='1', name='read_interview_scope', arguments={})])
            if self.n == 2:
                return AssistantResponse(tool_calls=[ToolCall(id='2', name='read_topics',
                                                              arguments={'topic_ids': [PRIVATE_ID]})])
            if self.n == 3:
                return AssistantResponse(tool_calls=[ToolCall(id='3', name='submit_interview_action',
                                                              arguments={'action': 'ask_question', 'topic_id': role,
                                                                         'question_id': role + '-base'})])
            self.feedback = json.loads(messages[-1].text)
            return AssistantResponse(tool_calls=[ToolCall(id='4', name='submit_interview_action',
                                                          arguments={'action': 'ask_question', 'topic_id': PRIVATE_ID,
                                                                     'question_id': PRIVATE_ID + '-base'})])

    reader = Reader()
    decision = asyncio.run(interview(s, reader, asyncio.Event()))
    assert reader.feedback['error'] == 'private_candidate_preferred'
    assert reader.feedback['preferred_candidate_ids'] == [PRIVATE_ID]
    assert set(reader.feedback['fallback_fields']) == {'fallback_reason', 'fallback_candidate_id'}
    assert reader.n == 4 and decision.topic_id == PRIVATE_ID


def test_lexical_preference_is_calibrated_on_representative_jds():
    """Plan §2.1.1 asks for positive/negative samples before trusting the score.

    Only the teaching point (title + base question) counts. A shared word in the reference or a
    coarse tag is not enough: live rag16 asked a cache-hit-rate question for a locks/idempotency
    JD because both mention Redis, and even an explicit 'fall back when the point differs' line
    (skill v2) did not change the model's choice. The gate is therefore tightened here.
    """
    from interview_agent.question_banks.ranking import preferred_private_ids

    def private(title, base, reference='', tags=()):
        return {'title': title, 'base': base, 'reference': reference, 'tags': list(tags),
                'bank_item': {'origin': 'private'}}

    topics = {
        'b-retrieval': private('检索召回与重排', '召回不准时你怎样定位？',
                               '向量召回、关键词召回与重排', ['检索']),
        'b-tx': private('事务边界', '事务为什么这样切？', 'rollback 与隔离级别'),
        'b-cache': private('缓存命中率优化', '命中率从 92% 掉到 70%，怎样定位？',
                           '淘汰策略与热点 key', ['Redis']),
        'role-topic': {'title': '工具治理', 'base': '工具参数要注意什么？', 'reference': '权限与预算'},
    }

    positive = 'AI 应用实习：熟悉 RAG 检索召回与向量检索优化。'
    assert preferred_private_ids(topics, jd_text=positive) == ['b-retrieval']

    unrelated = '土木造价实习：熟悉工程量清单与招投标流程。'
    assert preferred_private_ids(topics, jd_text=unrelated) == []

    # No query (old session) must not invent a preference.
    assert preferred_private_ids(topics) == []

    # The live false hit: JD wants locks/idempotency; the bank only mentions Redis in a tag and in
    # its reference. No candidate is the correct outcome, so the role pack stays in play.
    locks = 'AI 应用实习：熟悉 Redis 分布式锁与 HTTP 幂等设计。'
    assert preferred_private_ids(topics, jd_text=locks) == []

    # A real match in the base question still qualifies even without a tag.
    assert preferred_private_ids(
        {'b-lock': private('锁的归属与过期', 'Redis 锁只使用 SET NX 还需要考虑什么？')},
        jd_text='熟悉 Redis 分布式锁') == ['b-lock']


def test_the_scope_view_lists_preferred_candidates_and_role_backups():
    from test_interview_agent import scope as agent_scope

    from interview_agent.interviews.context import compile_interview_context

    s = agent_scope()
    s['training_context'] = {'jd_text': '熟悉 LangGraph 的状态恢复与 checkpointer', 'direction': 'AI 应用'}
    s['strategies'] = []
    authored = s['pack']
    source_id = authored['topic_order'][0]
    private = {**authored['topics'][source_id], 'title': 'LangGraph 的状态与恢复',
               'has_reference': False,
               'bank_item': {'origin': 'private', 'item_id': source_id, 'bank_id': 'b-1',
                             'version': 1, 'revision': 1, 'name': 'user-bank', 'enabled': True,
                             'item_count': 1}}
    s['pack'] = {**authored, 'topics': {**authored['topics'], PRIVATE_ID: private},
                 'topic_order': [PRIVATE_ID, *authored['topic_order']]}

    view = compile_interview_context(s)
    assert view['preferred_candidates'] == [{'id': PRIVATE_ID, 'title': 'LangGraph 的状态与恢复',
                                             'source': 'private_bank', 'has_reference': False}]
    assert view['alternatives'] and all(item['source'] == 'role_pack' for item in view['alternatives'])
    assert all(set(item) == {'id', 'title', 'source'} for item in view['alternatives'])
