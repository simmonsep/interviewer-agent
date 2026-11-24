"""F07Z-167 Phase 1: the full-simulation profile, its termination rules and skip.

The full profile is server-configured (12 rounds / 45 minutes), frozen from preparation into the
session, and the clock starts at the first published question. A normal finish needs ten asked
rounds unless the topic pack cannot support them or the closing window is open. Skipping records
an event with no observation; a racing answer and skip let only the first commit win.
"""
from uuid import uuid4

import pytest

from interview_agent.interviews.contracts import validate_decision
from test_interview_prep_repository import (base as prep_base, checkpoints, confirm as prep_confirm,
                                            context as prep_context, once as prep_once,
                                            view as prep_view)
from test_interview_repository import arun, base, context, first, post, process, question, view
from test_worker import worker_settings
from test_postgres import pg_settings, query  # noqa: F401  (fixtures re-exported)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def deadline_seconds(owner, session):
    rows = query(owner, 'SELECT extract(epoch FROM deadline_at-clock_timestamp()) AS n '
                        'FROM app.career_interviews WHERE id=%s', (session['id'],))
    return float(rows[0]['n'])


def test_old_client_keeps_its_own_numbers(context, worker_settings):
    """No profile field means the caller's 2 questions and the old policy; nothing is widened."""
    c, g = context
    s = first(c, g, worker_settings)
    assert s['policy']['max_questions'] == 2 and s['policy']['duration_seconds'] == 1200
    assert 'profile' not in s['policy']


def test_full_profile_is_server_configured_and_the_clock_starts_at_the_first_question(
        context, worker_settings, pg_settings):
    c, g = context
    created = post(c, base(g) + '/interviews',
                   {'expected_version': 1, 'role': 'java-backend', 'profile': 'full',
                    # Deliberately stale numbers: the server must ignore them for a full run.
                    'max_questions': 2, 'duration_seconds': 120})
    assert created.status_code == 202, created.text
    s = created.json()
    assert s['policy']['profile'] == 'full'
    assert s['policy']['max_questions'] == 12 and s['policy']['duration_seconds'] == 2700
    assert s['policy']['full_simulation'] == {'version': 'full-v1', 'target_low': 10,
                                              'target_high': 12, 'candidate_capacity': 8}
    # Four role topics contribute base + probe each; no resume facts, so the floor is eight.
    assert s['policy']['minimum_questions'] == 8
    # Before the first question the row carries the setup allowance, not the 45 minutes.
    assert deadline_seconds(pg_settings[1], s) > 2700

    arun(process(worker_settings))
    s = view(c, g, s)
    assert s['status'] == 'WAITING_FOR_USER'
    remaining = deadline_seconds(pg_settings[1], s)
    assert 2680 <= remaining <= 2700, remaining


def _finish_scope(asked, remaining):
    history = [{'id': f'00000000-0000-0000-0000-00000000000{i}', 'topic_id': 'sql-isolation',
                'question_id': f'sql-isolation-base-{i}', 'kind': 'base', 'payload': {}}
               for i in range(asked)]
    return {'policy': {'role': 'java-backend', 'mode': 'interview', 'version': 'ja05-policy-v1',
                       'max_questions': 12, 'duration_seconds': 2700, 'profile': 'full',
                       'minimum_questions': 10},
            'pack': {'topic_order': ['sql-isolation'], 'version': 'v',
                     'topics': {'sql-isolation': {'topic_id': 'sql-isolation', 'questions': [],
                                                  'references': [], 'origin': 'role_pack'}}},
            'question_history': history, 'current_question': history[-1], 'answer': None,
            'remaining_seconds': remaining, 'facts': [], 'target': {}, 'prior_observations': []}


def test_full_finish_below_target_is_refused_outside_the_closing_window():
    with pytest.raises(ValueError, match='minimum_coverage_not_met'):
        validate_decision(_finish_scope(9, 1000), {'action': 'finish'})
    # Ten asked rounds satisfy the target.
    assert validate_decision(_finish_scope(10, 1000), {'action': 'finish'}).action == 'finish'


def test_the_closing_window_allows_an_early_finish():
    assert validate_decision(_finish_scope(9, 120), {'action': 'finish'}).action == 'finish'


def test_skip_records_no_observation_and_the_next_round_still_counts(
        context, worker_settings):
    c, g = context
    s = first(c, g, worker_settings)
    skipped = question(s)['id']
    response = post(c, base(g) + f"/interviews/{s['id']}/skip",
                    {'expected_version': s['version'], 'question_id': skipped})
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'PENDING'

    arun(process(worker_settings))
    s = view(c, g, s)
    assert s['status'] == 'WAITING_FOR_USER'
    skips = [e for e in s['events'] if e['kind'] == 'skip']
    assert len(skips) == 1 and skips[0]['payload']['question_id'] == skipped
    assert s['observations'] == []

    assert post(c, base(g) + f"/interviews/{s['id']}/answers",
                {'expected_version': s['version'], 'question_id': question(s)['id'],
                 'text': '不知道，只加volatile。'}).status_code == 202
    arun(process(worker_settings))
    s = view(c, g, s)
    assert s['status'] == 'COMPLETED'
    assert s['report']['questions'] == 2 and s['report']['answers'] == 1
    assert s['report']['stop_reason'] == 'question_limit'


def test_skip_and_answer_race_lets_only_the_first_commit_win(context, worker_settings):
    c, g = context
    s = first(c, g, worker_settings)
    current = question(s)['id']
    version = s['version']
    skipped = post(c, base(g) + f"/interviews/{s['id']}/skip",
                   {'expected_version': version, 'question_id': current})
    assert skipped.status_code == 202
    late = post(c, base(g) + f"/interviews/{s['id']}/answers",
                {'expected_version': version, 'question_id': current, 'text': '不知道'})
    assert late.status_code == 409 and late.json()['code'] == 'version_conflict'


def test_skip_is_idempotent_with_the_same_key(context, worker_settings):
    c, g = context
    s = first(c, g, worker_settings)
    key = uuid4().hex
    body = {'expected_version': s['version'], 'question_id': question(s)['id']}
    first_call = post(c, base(g) + f"/interviews/{s['id']}/skip", body, key)
    replay = post(c, base(g) + f"/interviews/{s['id']}/skip", body, key)
    assert first_call.status_code == replay.status_code == 202
    assert first_call.json()['version'] == replay.json()['version']


def test_a_full_session_reaches_its_floor_and_finishes_cleanly(context, worker_settings,
                                                               pg_settings):
    """Eight authored candidates (four topics x base/probe), then a legal finish at the floor."""
    c, g = context
    s = first(c, g, worker_settings, profile='full')
    assert s['policy']['minimum_questions'] == 8
    for _ in range(10):
        if s['status'] == 'COMPLETED':
            break
        assert post(c, base(g) + f"/interviews/{s['id']}/answers",
                    {'expected_version': s['version'], 'question_id': question(s)['id'],
                     'text': '不知道，只加volatile。'}).status_code == 202
        arun(process(worker_settings))
        s = view(c, g, s)
    assert s['status'] == 'COMPLETED'
    assert s['report']['questions'] == 8 and s['report']['answers'] == 8
    assert s['report']['stop_reason'] == 'agent_finished'
    rows = query(pg_settings[1], 'SELECT model_requests,question_count FROM app.career_interviews '
                                 'WHERE id=%s', (s['id'],))
    sizes = query(pg_settings[1], 'SELECT octet_length(input_payload::text) AS n '
                                  'FROM app.career_interview_runs WHERE session_id=%s', (s['id'],))
    # Eight questions plus the finish decision, three tool rounds each.
    assert rows[0]['model_requests'] == 27
    assert max(row['n'] for row in sizes) < 60_000


def test_a_full_session_with_a_fact_reaches_twelve_rounds(context, worker_settings, pg_settings):
    """One usable fact gives four project rounds; with four topics the session reaches the cap."""
    from test_material_repository import analyze, confirm, source
    c, g = context
    source(c, g)
    confirm(c, g, analyze(c, g, worker_settings))
    materials = c.get(base(g) + '/materials').json()
    fact = materials['facts'][0]['id']
    s = first(c, g, worker_settings, fact_ids=[fact], profile='full')
    assert s['policy']['minimum_questions'] == 10
    focuses = []
    for _ in range(14):
        if s['status'] == 'COMPLETED':
            break
        focuses.append(question(s)['payload'].get('project_focus'))
        response = post(c, base(g) + f"/interviews/{s['id']}/answers",
                        {'expected_version': s['version'], 'question_id': question(s)['id'],
                         'text': '不知道，只加volatile。'})
        assert response.status_code == 202, (response.text, s['status'], s['version'])
        arun(process(worker_settings))
        s = view(c, g, s)
    assert s['status'] == 'COMPLETED'
    assert s['report']['questions'] == 12 and s['report']['answers'] == 12
    assert s['report']['stop_reason'] == 'question_limit'
    assert [focus for focus in focuses if focus][:4] == [
        'contribution', 'verification', 'failure', 'tradeoff']
    # Twelve questions plus the finishing decision, three tool rounds each.
    rows = query(pg_settings[1], 'SELECT model_requests FROM app.career_interviews WHERE id=%s',
                 (s['id'],))
    assert rows[0]['model_requests'] == 39


def test_clarification_restates_scope_without_changing_the_round(context, worker_settings):
    """F07Z-167: clarify is a restatement only: same question, same version, no round, no answer."""
    c, g = context
    s = first(c, g, worker_settings)
    current = question(s)
    version = s['version']
    key = uuid4().hex
    body = {'expected_version': version, 'question_id': current['id']}
    first_call = post(c, base(g) + f"/interviews/{s['id']}/clarify", body, key)
    replay = post(c, base(g) + f"/interviews/{s['id']}/clarify", body, key)
    assert first_call.status_code == replay.status_code == 202
    value = first_call.json()
    assert value['status'] == 'WAITING_FOR_USER' and value['version'] == version
    clarifications = [e for e in value['events'] if e['kind'] == 'clarification']
    assert len(clarifications) == 1
    assert '本题考查' in clarifications[0]['payload']['text']
    assert question(value)['id'] == current['id']
    wrong = post(c, base(g) + f"/interviews/{s['id']}/clarify",
                 {'expected_version': version, 'question_id': str(uuid4())})
    assert wrong.status_code == 409 and wrong.json()['code'] == 'question_changed'
    # The round is still the same one and can be answered normally after the clarification.
    assert post(c, base(g) + f"/interviews/{s['id']}/answers",
                {'expected_version': version, 'question_id': current['id'],
                 'text': '不知道，只加volatile。'}).status_code == 202
    arun(process(worker_settings))
    s = view(c, g, s)
    assert s['status'] in {'WAITING_FOR_USER', 'COMPLETED'}


def test_preparation_freezes_the_full_profile_into_the_session(prep_context, worker_settings):
    client, goal, _uid = prep_context
    prep = post(client, prep_base(goal) + '/interview-preparations',
                {'expected_version': 1, 'role': 'java-backend', 'profile': 'full'}).json()
    assert prep['profile'] == 'full'
    assert prep['max_questions'] == 12 and prep['duration_seconds'] == 2700
    assert arun(prep_once(worker_settings))
    prep = prep_view(client, goal, prep['id'])
    assert prep['plan']['max_questions'] == 12

    prep_confirm(client, goal, prep)
    assert arun(prep_once(worker_settings))
    done = prep_view(client, goal, prep['id'])
    assert done['status'] == 'SUCCEEDED' and done['session_id']
    session = client.get(prep_base(goal) + f"/interviews/{done['session_id']}").json()
    assert session['policy']['profile'] == 'full'
    assert session['policy']['max_questions'] == 12
    assert session['policy']['duration_seconds'] == 2700
