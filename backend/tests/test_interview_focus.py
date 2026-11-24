"""F07Z-165 A: a review's retest suggestion can open a focused coaching session."""
import pytest
from uuid import uuid4

from test_interview_repository import (answer, base, completed, context, create, post,  # noqa: F401
                                       process, question, view)
from test_postgres import arun, pg_settings  # noqa: F401
from test_worker import worker_settings  # noqa: F401


def suggestion_of(session):
    items = session['report']['retest']
    assert items, 'the scripted source session must yield a retest suggestion'
    return items[0]


def focus(c, goal, source, item, **overrides):
    body = {'expected_version': 1, 'role': 'java-backend', 'mode': 'coaching', 'max_questions': 2,
            'focus_session_id': source['id'], 'focus_observation_id': item['observation_id'],
            **overrides}
    return post(c, base(goal) + '/interviews', body)


@pytest.mark.postgres
@pytest.mark.redis
def test_focus_first_question_is_the_frozen_retest_and_costs_no_model_round(context, worker_settings):
    c, g = context
    source = completed(c, g, worker_settings, mode='coaching')
    item = suggestion_of(source)
    created = focus(c, g, source, item)
    assert created.status_code == 202, created.text
    focused = created.json()
    arun(process(worker_settings))
    focused = view(c, g, focused)

    q = question(focused)
    assert q['payload']['kind'] == 'retest'
    assert q['payload']['text'] == item['question']
    assert q['payload']['source'] == 'retest_suggestion'
    assert q['payload']['focus']['observation_id'] == item['observation_id']
    assert q['payload']['focus']['source_session_id'] == source['id']
    assert q['payload']['focus']['source_pack_version'] == item['pack_version']
    assert focused['run']['model_requests'] == 0, 'the frozen first question must not spend a model round'

    # The source session is read-only: same report, same question count.
    reread = view(c, g, source)
    assert reread['report']['retest'] == source['report']['retest']
    assert reread['report']['questions'] == source['report']['questions']

    # After the answer the ordinary coaching loop continues.
    assert answer(c, g, focused, '我会把检查与更新放在一个事务里，并说明失败边界。').status_code == 202
    arun(process(worker_settings))
    focused = view(c, g, focused)
    assert focused['status'] in ('WAITING_FOR_USER', 'COMPLETED')


@pytest.mark.postgres
@pytest.mark.redis
def test_focus_requires_coaching_and_a_valid_source(context, worker_settings):
    c, g = context
    source = completed(c, g, worker_settings, mode='coaching')
    item = suggestion_of(source)

    assert focus(c, g, source, item, mode='interview').status_code == 400
    assert focus(c, g, source, item, include_practice_mistakes=True).status_code == 400
    missing = post(c, base(g) + '/interviews',
                   {'expected_version': 1, 'role': 'java-backend', 'mode': 'coaching',
                    'focus_session_id': source['id']})
    assert missing.status_code == 400

    unknown = focus(c, g, source, {'observation_id': str(uuid4())})
    assert unknown.status_code == 404 and unknown.json()['code'] == 'focus_observation_unavailable'
    no_session = focus(c, g, {'id': str(uuid4())}, item)
    assert no_session.status_code == 404 and no_session.json()['code'] == 'focus_source_unavailable'


@pytest.mark.postgres
@pytest.mark.redis
def test_retracted_or_other_goal_observations_are_rejected(context, worker_settings):
    c, g = context
    source = completed(c, g, worker_settings, mode='coaching')
    item = suggestion_of(source)

    other = post(c, '/api/career-goals/', {'message': '另找方向，用来验证跨目标读取。',
                                          'conditions': {'direction': 'Java'}})
    assert other.status_code in (200, 202)
    cross = focus(c, other.json(), source, item)
    assert cross.status_code == 404 and cross.json()['code'] == 'focus_source_unavailable'

    retracted = post(c, base(g) + f"/interview-memory/{item['observation_id']}/retract",
                     {'reason': '这条观察引用了回答，但我认为结论需要更正。'})
    assert retracted.status_code == 200, retracted.text
    blocked = focus(c, g, source, item)
    assert blocked.status_code == 409 and blocked.json()['code'] == 'focus_observation_not_retestable'
