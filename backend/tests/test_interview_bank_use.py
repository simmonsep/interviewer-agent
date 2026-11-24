"""新增业务 1: the interviewer may declare the selected bank irrelevant, but not while using it.

These are pure contract tests: no database, no model.
"""
import pytest

from interview_agent.interviews.catalog import pack
from interview_agent.interviews.contracts import Decision, validate_decision


def scope():
    return {
        'policy': {'role': 'ai-application', 'mode': 'interview', 'max_questions': 4,
                   'version': 'ja05-policy-v1', 'allow_project_questions': False},
        'facts': [], 'answer': None, 'current_question': None, 'question_history': [],
        'pack': {'version': 'test-pack', 'topics': {
            'agent-tools': {'title': '工具契约', 'base': '问题', 'probe': '追问', 'retest': '复测',
                            'references': [{'id': 'agent-tools-reference', 'text': '参考', 'origin': 'bank_declared_reference'}],
                            'has_reference': True,
                            'questions': [
                                {'id': 'agent-tools-base', 'kind': 'base', 'text': '问题'},
                                {'id': 'agent-tools-probe', 'kind': 'probe', 'text': '追问'},
                                {'id': 'agent-tools-retest', 'kind': 'retest', 'text': '复测'},
                            ]},
            'b-0123456789': {'title': '库里的题', 'base': '库问题', 'probe': '', 'retest': '',
                             'references': [{'id': 'b-ref', 'text': '库参考', 'origin': 'bank_declared_reference'}],
                             'has_reference': True,
                             'questions': [{'id': 'b-0123456789-base', 'kind': 'base', 'text': '库问题'}]},
        }, 'topic_order': ['agent-tools', 'b-0123456789']},
    }


def decision(**overrides):
    payload = {'action': 'ask_question', 'topic_id': 'agent-tools', 'question_id': 'agent-tools-base'}
    payload.update(overrides)
    return Decision.model_validate(payload)


def test_default_is_unknown_so_older_callers_keep_working():
    assert decision().bank_use == 'unknown'


def test_declaring_not_relevant_and_using_the_bank_is_rejected():
    with pytest.raises(ValueError, match='bank_declared_not_relevant_but_used'):
        validate_decision(scope(), decision(topic_id='b-0123456789', question_id='b-0123456789-base',
                                            bank_use='not_relevant'))


def real_scope():
    """The role's authored pack, so catalog.topic() sees exactly what production sees."""
    authored = pack('ai-application')
    topic_id = authored['topic_order'][0]
    return {
        'pack': authored,
        'policy': {'role': 'ai-application', 'mode': 'interview', 'max_questions': 4,
                   'version': 'ja05-policy-v1', 'allow_project_questions': False},
        'facts': [], 'answer': None, 'current_question': None, 'question_history': [],
    }, topic_id


def test_declaring_not_relevant_while_using_the_role_pack_is_allowed():
    scope, topic_id = real_scope()
    question_id = topic_id + '-base'
    validate_decision(scope, Decision.model_validate({
        'action': 'ask_question', 'topic_id': topic_id, 'question_id': question_id,
        'bank_use': 'not_relevant'}))


def real_scope_with_bank_topic():
    """A b- topic with the exact shape of a real one (copied and re-keyed), so catalog.topic()
    behaves as it does for an imported bank."""
    authored, topic_id = real_scope()
    source = authored['pack']['topics'][topic_id]
    bank_id = 'b-0123456789abcdef'
    # The authored pack carries base/probe/retest, not a questions list - catalog.topic() builds
    # the questions, so the clone only needs the same keys under a b- topic id.
    clone = {**source}
    authored['pack'] = {**authored['pack'],
                        'topics': {**authored['pack']['topics'], bank_id: clone},
                        'topic_order': [*authored['pack']['topic_order'], bank_id]}
    return authored, bank_id


def test_declaring_used_while_using_the_bank_is_allowed():
    scope, bank_id = real_scope_with_bank_topic()
    validate_decision(scope, Decision.model_validate({
        'action': 'ask_question', 'topic_id': bank_id, 'question_id': bank_id + '-base',
        'bank_use': 'used'}))


def test_declaring_not_relevant_while_using_the_bank_is_rejected_with_the_real_shape():
    scope, bank_id = real_scope_with_bank_topic()
    with pytest.raises(ValueError, match='bank_declared_not_relevant_but_used'):
        validate_decision(scope, Decision.model_validate({
            'action': 'ask_question', 'topic_id': bank_id, 'question_id': bank_id + '-base',
            'bank_use': 'not_relevant'}))
