"""Persist real question events, review versions, and reuse on an independent preparation."""
import json

import httpx
import pytest

from interview_agent.connectors.public_reference import PreparationReference
from test_interview_prep_planning import FakePlanner, once, plan_of, run_mock_plan
from test_interview_prep_repository import base, checkpoints, confirm, context, create, view
from test_interview_repository import answer, process, question, view as session_view
from test_postgres import arun, client_for, pg_settings, query, signup
from test_preparation_reference import MARKDOWN, PASSAGE, TOPIC
from test_worker import worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]
WIKI = '/api/runtime/interview-wiki'
JD = '使用 LangGraph 实现中断恢复，需要处理写入副作用。'
QUESTION = '中断前已经把请求写入存储，恢复时如何避免重复写入？'


def goal_for(client, jd=JD):
    from test_interview_prep_repository import post
    response = post(client, '/api/career-goals/', {
        'message': '按岗位做一次完整面试', 'jd_text': jd,
        'conditions': {'direction': 'AI 应用开发'},
    })
    assert response.status_code == 202, response.text
    return response.json()


def with_source(client, owner, settings, goal, tmp_path, wording=QUESTION):
    lookup = PreparationReference(lambda: True, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=MARKDOWN)))
    plan = {'generated_questions': [{
        'title': '中断恢复时的重复执行',
        'question': wording, 'probe': '哪一步适合检查幂等键？',
        'target_quote': '中断恢复', 'source_id': TOPIC, 'source_quote': PASSAGE,
    }]}
    result = run_mock_plan(settings, owner, client, goal, tmp_path,
                           json.dumps(plan), requests=[], reference_lookup=lookup,
                           role='ai-application', profile='full', use_resume=False)
    assert result['status'] == 'WAITING_FOR_USER', result['error_code']
    assert result['plan']['public_reference_count'] == 1
    return result


def start(client, settings, goal, plan):
    response = confirm(client, goal, plan)
    assert response.status_code == 200, response.text
    assert arun(once(settings))
    started = view(client, goal, plan['id'])
    assert started['status'] == 'SUCCEEDED', started.get('error_code')
    arun(process(settings))
    return session_view(client, goal, {'id': started['session_id']})


def review(client, item, action, expected=None):
    return client.post(WIKI + f"/{item['id']}/review",
                       json={'action': action, 'expected_active_id': expected})


def prepare(client, settings, goal, planner):
    response = create(client, goal, role='ai-application', profile='full', use_resume=False)
    assert response.status_code == 202, response.text
    assert arun(once(settings, lambda claim: planner))
    return view(client, goal, response.json()['id'])


def items(client):
    response = client.get(WIKI)
    assert response.status_code == 200, response.text
    return response.json()['items']


def test_real_question_event_becomes_private_wiki_and_is_reused_without_planner(
        context, worker_settings, pg_settings, tmp_path):
    client, _, _user = context
    goal = goal_for(client)
    first_plan = with_source(client, pg_settings[1], worker_settings, goal, tmp_path)
    assert items(client) == []
    original = start(client, worker_settings, goal, first_plan)
    original_question = question(original)
    assert original_question['payload']['text'] == QUESTION
    [draft] = items(client)
    assert draft['status'] == 'pending' and draft['source_event_id'] == original_question['id']
    assert draft['document']['public_reference']['quote'] == PASSAGE
    assert 'target_quote' not in draft['document']
    pre_publish = FakePlanner(result=plan_of())
    pending_plan = prepare(client, worker_settings, goal_for(client), pre_publish)
    assert pre_publish.calls == 1 and 'wiki_question_count' not in pending_plan['plan']

    with client_for(pg_settings[0]) as other:
        other.headers['Origin'] = pg_settings[0].origin
        assert other.get(WIKI).status_code == 401
        other_user = signup(other)['user']['id']
        try:
            assert items(other) == []
            assert review(other, draft, 'publish').status_code == 404
            other_plan = FakePlanner(result=plan_of())
            prepare(other, worker_settings, goal_for(other), other_plan)
            assert other_plan.calls == 1
        finally:
            query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (other_user,))

    assert review(client, draft, 'publish').json()['status'] == 'active'
    assert review(client, draft, 'publish').json()['status'] == 'active'
    changed_goal = goal_for(client, '面向 LangGraph 的人工确认和中断恢复。')
    unused_planner = FakePlanner(result=plan_of())
    reuse = prepare(client, worker_settings, changed_goal, unused_planner)
    assert not unused_planner.calls
    assert reuse['plan']['planner'] == 'wiki-reuse'
    assert reuse['plan']['wiki_question_count'] == 1 and reuse['plan']['generated_question_count'] == 0
    assert QUESTION not in str(reuse)
    session = start(client, worker_settings, changed_goal, reuse)
    asked = question(session)['payload']
    assert asked['source'] == 'wiki_reused' and asked['wiki']['id'] == draft['id']
    assert asked['text'] == QUESTION and asked['has_reference'] is False
    assert [item['id'] for item in items(client)] == [draft['id']]
    for _ in range(10):
        submitted = answer(client, changed_goal, session, '我会核对检查点和写入记录，验证恢复后的最终状态。')
        assert submitted.status_code == 202, submitted.text
        arun(process(worker_settings))
        session = session_view(client, changed_goal, session)
        assert session['status'] in ('WAITING_FOR_USER', 'COMPLETED'), session['run']
    assert session['status'] == 'COMPLETED' and session['report']['questions'] == 10
    assert session['report']['wiki_questions'] == 2
    assert session['report']['generated_questions'] == 0
    assert session['report']['public_references'][0]['quote'] == PASSAGE


def test_one_wiki_hit_does_not_skip_generation_for_a_different_requirement(
        context, worker_settings, pg_settings, tmp_path):
    client, _, _user = context
    seed = goal_for(client)
    owner = pg_settings[1]
    start(client, worker_settings, seed, with_source(client, owner, worker_settings, seed, tmp_path))
    [draft] = items(client)
    assert review(client, draft, 'publish').status_code == 200

    goal = goal_for(client, 'LangGraph 中断恢复；量化向量索引召回损失。')
    requests = []
    extra = '怎样用精确近邻结果作为基线判断向量索引是否漏召回？'
    plan = run_mock_plan(worker_settings, owner, client, goal, tmp_path,
                         json.dumps({'generated_questions': [{
                             'title': '向量召回损失', 'target_quote': '量化向量索引召回损失',
                             'question': extra,
                         }]}), requests=requests, role='ai-application', profile='full', use_resume=False)
    assert plan['status'] == 'WAITING_FOR_USER', plan['error_code']
    assert len(requests) == 2
    assert plan['plan']['wiki_question_count'] == plan['plan']['generated_question_count'] == 1
    assert 'wiki' in plan['plan']['jd_coverage'][0]['planned_sources']
    assert plan['plan']['jd_coverage'][1]['planned_sources'] == ['jd_generated']
    session = start(client, worker_settings, goal, plan)
    assert question(session)['payload']['source'] == 'wiki_reused'
    for _ in range(3):
        if any(event['kind'] == 'question' and event['payload']['text'] == extra for event in session['events']):
            break
        assert answer(client, goal, session, '我会核对题目里的条件，再验证两种路径。').status_code == 202
        arun(process(worker_settings))
        session = session_view(client, goal, session)
    else:
        pytest.fail('the requirement-specific generated question was not asked')


def test_versions_deduplicate_and_withdraw_prepared_or_open_sessions(
        context, worker_settings, pg_settings, tmp_path):
    client, _, _user = context
    owner = pg_settings[1]
    for wording in (QUESTION, QUESTION, '外部审批恢复后，怎样核对数据库里的执行记录？'):
        goal = goal_for(client)
        start(client, worker_settings, goal,
              with_source(client, owner, worker_settings, goal, tmp_path, wording))
    new, old = items(client)
    assert [new['version'], old['version']] == [2, 1]
    assert review(client, old, 'publish').json()['status'] == 'active'
    assert review(client, new, 'publish').json()['code'] == 'wiki_version_conflict'
    assert review(client, new, 'publish', old['id']).json()['status'] == 'active'
    assert review(client, old, 'restore', new['id']).json()['status'] == 'active'
    assert items(client)[0]['status'] == 'archived'

    pending_goal = goal_for(client)
    waiting = prepare(client, worker_settings, pending_goal, FakePlanner(result=plan_of()))
    opened_goal = goal_for(client)
    plan = prepare(client, worker_settings, opened_goal, FakePlanner(result=plan_of()))
    opened = start(client, worker_settings, opened_goal, plan)
    asked = question(opened)
    assert review(client, old, 'withdraw', old['id']).json()['status'] == 'withdrawn'
    assert view(client, pending_goal, waiting['id'])['error_code'] == 'wiki_source_withdrawn'
    history = session_view(client, opened_goal, opened)
    assert history['status'] == 'STALE' and history['report']['stop_reason'] == 'wiki_source_withdrawn'
    assert question(history) == asked
    no_reuse = FakePlanner(result=plan_of())
    replaced = prepare(client, worker_settings, goal_for(client), no_reuse)
    assert no_reuse.calls == 1 and 'wiki_question_count' not in replaced['plan']
    assert review(client, old, 'restore').json()['code'] == 'wiki_version_not_reviewable'
