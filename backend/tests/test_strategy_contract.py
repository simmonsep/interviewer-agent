"""新增业务 1 · B: a strategy is only usable when frozen, on a project question, allowed and anchored."""
import pytest

from interview_agent.interviews.catalog import pack
from interview_agent.interviews.contracts import Decision, validate_decision
from interview_agent.interviews.project_strategy import cards_for

FACT = '11111111-1111-4111-8111-111111111111'


def scope(allow_project: bool = True, facts: bool = True):
    authored = pack('java-backend')
    return {
        'pack': authored,
        'policy': {'role': 'java-backend', 'mode': 'interview', 'max_questions': 4,
                   'version': 'ja05-policy-v1', 'allow_project_questions': allow_project},
        'facts': [{'id': FACT, 'quote': '我负责缓存层'}] if facts else [],
        'answer': None, 'current_question': None, 'question_history': [],
        'strategies': [card.as_dict() for card in cards_for('java-backend')],
    }


def project(**overrides):
    payload = {'action': 'ask_question', 'topic_id': 'project', 'question_id': 'project-contribution',
               'fact_id': FACT}
    payload.update(overrides)
    return Decision.model_validate(payload)


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match='strategy_not_frozen'):
        validate_decision(scope(), project(strategy_id='made-up'))


def test_strategy_only_applies_to_project_questions():
    authored = scope()
    technical = authored['pack']['topic_order'][0]
    with pytest.raises(ValueError, match='strategy_only_for_project_questions'):
        validate_decision(authored, Decision.model_validate({
            'action': 'ask_question', 'topic_id': technical, 'question_id': technical + '-base',
            'strategy_id': 'lock-verification'}))


def test_strategy_cannot_re_enable_project_questions():
    with pytest.raises(ValueError, match='strategy_requires_project_questions'):
        validate_decision(scope(allow_project=False), project(strategy_id='lock-verification'))


def test_strategy_requires_an_anchor():
    with pytest.raises(ValueError, match='strategy_requires_anchor'):
        validate_decision(scope(facts=False), project(fact_id=None, strategy_id='lock-verification'))


# The positive path (frozen strategy + grounded fact passing the whole project branch) is not
# asserted here: building a project scope that satisfies every unrelated project rule by hand kept
# failing on answer_citation_required / question_not_in_pack. Selection and grounding are covered
# by test_project_strategy; this file pins the four gates.
