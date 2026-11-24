"""S02A: deterministic context compilation, stable clues, mode isolation, protected fields."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from interview_agent.interviews.agent import ScriptedInterviewer, interview
from interview_agent.interviews.billing import InterviewPolicy
from interview_agent.interviews.context import (
    CONTEXT_VERSION, compile_interview_context, cross_session, this_session)
from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
from test_billing import reply
from test_interview_agent import scope


def long_session(mode='interview', turns=8):
    """Six-plus questions where the first topic left the recent window long ago."""
    state = scope(mode, answer='这条回答保持原样：我负责接口与缓存层。')
    first = dict(state['current_question'])
    history = [dict(first, id=str(uuid4()), topic_id='java-concurrency', question_id='java-concurrency-base',
                    text='volatile 计数器的 count++ 在多线程下是否安全？' * 3)]
    # Every later turn uses a different real pack topic, so the first one leaves the window.
    for index in range(1, turns):
        history.append(dict(first, id=str(uuid4()), topic_id='sql-isolation',
                            question_id=f'sql-isolation-base-{index}',
                            text=f'第 {index} 个问题的题干。' * 4))
    state['question_history'] = history
    state['current_question'] = history[-1]
    state['prior_observations'] = [
        {'id': 'obs-early', 'source_session_id': 'session-old', 'answer_id': 'answer-early',
         'answer_quote': '上一场原答节选', 'topic_id': 'java-concurrency', 'label': 'needs_probe',
         'pack_version': 'pack-old', 'summary': '上一场观察：接口幂等仍需核验', 'origin': 'prior_observation_to_retest',
         'assisted': False},
        {'id': 'obs-own', 'source_session_id': state['current_question'].get('session_id', 'this'),
         'answer_id': state['answer']['id'], 'answer_quote': '这条回答保持原样', 'topic_id': 'java-concurrency',
         'label': 'inconclusive', 'pack_version': 'pack-current', 'summary': '本场观察',
         'origin': 'this_session_observation', 'assisted': False},
    ]
    return state


def test_the_same_snapshot_always_compiles_to_the_same_view():
    state = long_session()
    first = compile_interview_context(state)
    second = compile_interview_context(state)
    assert first == second
    assert first['context_version'] == CONTEXT_VERSION
    # No clock, no model: the view carries the frozen deadline unchanged (absent here).
    assert first['working_state']['deadline_at'] is None


def test_an_early_topic_keeps_ids_after_it_leaves_the_recent_window():
    # Coaching mode: the clue comes from another session, which a formal interview must not read.
    state = long_session(mode='coaching', turns=8)
    view = compile_interview_context(state)
    recent = {item['topic_id'] for item in view['question_history']}
    assert 'java-concurrency' not in recent and len(view['question_history']) == 6
    assert 'question_history.latest_6_only' in view['model_view_truncation']
    clue = next(item for item in view['working_state']['unresolved_clues'] if item['observation_id'] == 'obs-early')
    assert clue['topic_id'] == 'java-concurrency' and clue['answer_id'] == 'answer-early'
    assert 'java-concurrency' in view['working_state']['asked_topics']
    assert len(view['working_state']['asked_question_ids']) == 8
    assert view['working_state']['remaining_questions'] == state['policy']['max_questions'] - 8
    # A clue is IDs, never a model-written summary of the old answer.
    assert 'summary' not in clue and 'answer_quote' not in clue


@pytest.mark.parametrize('mode,cross_visible', [('interview', False), ('coaching', True)])
def test_mode_decides_whether_cross_session_material_is_readable(mode, cross_visible):
    state = long_session(mode=mode)
    view = compile_interview_context(state)
    assert view['mode'] == mode
    if cross_visible:
        assert [item['id'] for item in view['prior_evidence']] == ['obs-early']
        assert 'prior_evidence.mode_isolated' not in view['model_view_truncation']
    else:
        assert view['prior_evidence'] == []
        assert 'prior_evidence.mode_isolated' in view['model_view_truncation']
        assert 'obs-early' not in json.dumps(view, ensure_ascii=False)


def test_this_session_observations_stay_visible_in_a_formal_interview():
    state = long_session(mode='interview')
    view = compile_interview_context(state)
    assert [item['id'] for item in view['prior_observations']] == ['obs-own']
    assert [item['id'] for item in cross_session(state)] == ['obs-early']
    assert [item['id'] for item in this_session(state)] == ['obs-own']


def test_a_tight_budget_drops_loose_material_but_never_the_answer_or_facts():
    state = long_session(mode='coaching')
    state['facts'] = [{'id': str(uuid4()), 'quote': '用户确认的事实。' * 40,
                       'verification': 'user_confirmed_not_external_verified'}]
    full = compile_interview_context(state)
    tight = compile_interview_context(state, budget_bytes=1200)
    assert tight['answer'] == full['answer'] and tight['facts'] == full['facts']
    assert tight['current_question'] == full['current_question']
    assert any(reason.startswith('budget.') for reason in tight['model_view_truncation'])
    assert len(json.dumps(tight, ensure_ascii=False, separators=(',', ':')).encode()) <= 1200 or \
        'budget.exhausted' in tight['model_view_truncation']


def test_an_unknown_origin_row_is_treated_as_current_session_not_as_cross_session():
    state = long_session()
    state['prior_observations'][0].pop('origin')
    assert cross_session(state) == []
    assert [item['id'] for item in this_session(state)] == ['obs-early', 'obs-own']


def model_requests(state, project_root):
    """Run one real graph against a MockTransport and keep every serialised request body."""
    bodies = []
    policy = InterviewPolicy.load(project_root / 'configs/ja05-deepseek-flash.json')

    class Ledger:
        def __init__(self):
            self.policy = policy

        async def reserve(self, *args, **kwargs):
            return str(uuid4())

        async def settle(self, *args, **kwargs):
            return True

    async def prime():
        # The scripted interviewer is deterministic; prime its sequence outside the loop so the
        # sync MockTransport handler never tries to run another event loop.
        scripted = ScriptedInterviewer(state, lambda: asyncio.sleep(0))
        return [await scripted.complete([], []) for _ in range(8)]

    primed = asyncio.run(prime())
    remaining = iter(primed)

    async def scenario():
        def handler(request):
            bodies.append(request.content.decode('utf-8'))
            return httpx.Response(200, json=reply(next(remaining)))

        provider = DeepSeekProvider('offline-not-real',
                                    BudgetedTransport(httpx.MockTransport(handler), Ledger(), None, state))
        try:
            await interview(state, provider, asyncio.Event())
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    return bodies


@pytest.mark.parametrize('mode,cross_visible', [('interview', False), ('coaching', True)])
def test_the_actual_model_request_follows_the_mode_contract(mode, cross_visible, project_root):
    marker = 'SYNTHETIC-CROSS-SESSION-MARK'
    state = long_session(mode=mode)
    state['prior_observations'][0]['summary'] = marker
    bodies = model_requests(state, project_root)
    assert bodies and all(len(body.encode('utf-8')) <= 32768 for body in bodies)
    assert (marker in ' '.join(bodies)) is cross_visible
    current = next(body for body in bodies if state['answer']['text'] in body)
    assert state['answer']['text'] in current
