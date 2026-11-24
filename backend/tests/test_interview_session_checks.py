"""收尾 04: the retest suggestion check is a property, so it can be pinned without a database.

A suggestion may only point at a `needs_probe` observation of the same session, must not cite a
retracted one, and must carry a real question. The authored text itself is not asserted here -
that belongs to the question pack, not to this check.
"""
from interview_agent.evaluation.interview_session import retest_matches_observations


def observation(**overrides):
    base = {'id': 'o1', 'label': 'needs_probe', 'correction': None}
    base.update(overrides)
    return base


def suggestion(**overrides):
    base = {'observation_id': 'o1', 'question': '支付接口丢失响应后，客户端重试怎么避免重复下单？'}
    base.update(overrides)
    return base


def test_well_formed_suggestion_passes():
    assert retest_matches_observations({'retest': [suggestion()]}, [observation()]) is True


def test_no_suggestion_is_vacuously_true():
    assert retest_matches_observations({'retest': []}, [observation()]) is True
    assert retest_matches_observations(None, []) is True


def test_suggestion_for_an_unknown_observation_fails():
    assert retest_matches_observations({'retest': [suggestion(observation_id='other')]}, [observation()]) is False


def test_suggestion_for_a_supported_observation_fails():
    assert retest_matches_observations({'retest': [suggestion()]},
                                       [observation(label='supported_in_this_answer')]) is False
    assert retest_matches_observations({'retest': [suggestion()]},
                                       [observation(label='inconclusive')]) is False


def test_suggestion_for_a_retracted_observation_fails():
    assert retest_matches_observations({'retest': [suggestion()]},
                                       [observation(correction='用户指出该观察不成立')]) is False


def test_suggestion_without_a_question_fails():
    assert retest_matches_observations({'retest': [suggestion(question='  ')]}, [observation()]) is False
