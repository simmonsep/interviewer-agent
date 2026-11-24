"""S05 follow-up: the context A/B against the real pre-S02A baseline (0 cost).

The product has one model view per version, so "old truncation vs structured compile" cannot be
a runtime switch. The baseline is therefore frozen verbatim in
`benchmarks/interview-context-v0.1/legacy.py` at `b1c657a^` (S02A's parent), and this file runs
both implementations on the same frozen snapshot.

What is measured: after the sixth turn, does the payload still carry a way back to the earlier
turns? The old view kept the latest six questions and nothing else; the new one adds a
`working_state` whose `asked_topics` / `asked_question_ids` / `unresolved_clues` keep the ids of
everything asked. Both must keep the current answer and the confirmed facts untouched.
"""
import importlib.util
import json
import sys
from pathlib import Path

from interview_agent.interviews.context import compile_interview_context
from test_interview_agent import scope

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / 'benchmarks' / 'interview-context-v0.1' / 'legacy.py'


def legacy_view():
    """Import the frozen baseline as a module of the same package (relative imports work)."""
    name = 'interview_agent.interviews._context_baseline'
    spec = importlib.util.spec_from_file_location(name, BASELINE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.scope_view


def snapshot(turns=12):
    """Twelve answered turns: the first six are far outside any six-turn window."""
    state = scope('interview', answer='这条回答保持原样：我负责接口与缓存层，并给出验证过程。')
    history = []
    observations = []
    for index in range(turns):
        early = index < turns // 2
        question_id = f'turn-{index + 1:02d}'
        topic_id = 'java-concurrency' if early else 'sql-isolation'
        history.append({'id': question_id, 'topic_id': topic_id, 'question_id': question_id + '-base',
                        'text': f'第 {index + 1} 题：请说明这个主题的实现取舍。' * 3,
                        'kind': 'base' if index % 2 == 0 else 'probe'})
        observations.append({'id': f'obs{index + 1}', 'topic_id': topic_id, 'answer_id': f'a{index + 1}',
                             'label': 'needs_probe', 'assisted': False,
                             'origin': 'this_session_observation',
                             'summary': f'第 {index + 1} 条观察：还需要复测。' * 3,
                             'answer_quote': f'第 {index + 1} 题的原答节选。' * 2})
    state['question_history'] = history
    state['current_question'] = history[-1]
    state['prior_observations'] = observations
    return state


def refs(payload, ids):
    """How many of `ids` can be found anywhere in the serialised payload."""
    text = json.dumps(payload, ensure_ascii=False)
    return sum(1 for identity in ids if identity in text)


def test_the_structured_view_keeps_a_way_back_to_turns_the_window_dropped():
    state = snapshot()
    early = [f'turn-{index + 1:02d}' for index in range(6)]
    legacy = legacy_view()(state)
    current = compile_interview_context(state)

    assert refs(legacy, early) == 0, 'the baseline unexpectedly kept early question ids'
    assert refs(current, early) == 6, 'the compiled view lost an early question id'
    assert current['working_state']['asked_question_ids'][:6] == early
    assert current['working_state']['asked_topics'] == ['java-concurrency', 'sql-isolation']
    assert 'java-concurrency' not in json.dumps(legacy, ensure_ascii=False)

    # The things that must never change between versions.
    assert legacy['answer'] == current['answer'] == state['answer']
    assert legacy['facts'] == current['facts'] == state['facts']
    assert legacy['current_question'] == current['current_question']
    assert 'question_history.latest_6_only' in legacy['model_view_truncation']
    assert 'question_history.latest_6_only' in current['model_view_truncation']

    # Only the new view names its own contract and mode.
    assert 'context_version' not in legacy and 'mode' not in legacy and 'working_state' not in legacy
    assert current['context_version'] == 'interview-context-v1' and current['mode'] == 'interview'

    print(json.dumps({
        'turns': len(state['question_history']),
        'early_question_ids': len(early),
        'baseline_early_refs': refs(legacy, early),
        'compiled_early_refs': refs(current, early),
        'baseline_bytes': len(json.dumps(legacy, ensure_ascii=False).encode()),
        'compiled_bytes': len(json.dumps(current, ensure_ascii=False).encode()),
        'baseline_early_topic_visible': 'java-concurrency' in json.dumps(legacy, ensure_ascii=False),
        'compiled_working_state_topics': current['working_state']['asked_topics'],
    }, ensure_ascii=False, indent=1))
