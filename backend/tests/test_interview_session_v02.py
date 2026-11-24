"""收尾 04: v0.2 adds one protocol check and proves it on the branch that can satisfy it.

v0.1 stays frozen; this suite is a copy whose baseline declares
`retest_suggestion_matches_observation` and whose IS03 oracle requires it. IS03 is the technical
error branch (`只加volatile`), so the scripted protocol records `needs_probe` and the review must
resolve a suggestion for that observation.
"""
import os
from pathlib import Path

import pytest

from interview_agent.evaluation.interview_session import (
    SessionAttempt, execute_session, grade_session, load_session_suite)
from interview_agent.worker.config import WorkerSettings

from test_postgres import pg_settings  # noqa: F401  (pytest fixture reuse)

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / 'benchmarks/interview-session-v0.2'
CHECK = 'retest_suggestion_matches_observation'


def test_v02_is_well_formed_and_v01_is_untouched():
    manifest, cases, oracles, baseline = load_session_suite(SUITE)
    assert manifest['version'] == 'interview-session-v0.2'
    assert manifest['cases'] == 13
    assert CHECK in baseline['protocol_checks']
    assert CHECK in oracles['IS03'].protocol_checks
    # Only the weak branch declares it; a vacuous pass elsewhere carries no information.
    assert all(CHECK not in o.protocol_checks for cid, o in oracles.items() if cid not in ('IS03', 'IS11', 'IS12', 'IS13'))
    old_manifest, *_ = load_session_suite(ROOT / 'benchmarks/interview-session-v0.1')
    assert old_manifest['version'] == 'interview-session-v0.1'
    assert CHECK not in old_manifest['sha256']  # unchanged file list, proves v0.1 was not edited


@pytest.fixture(scope='session')
def worker_settings():
    path = os.environ.get('PY_INTERVIEW_WORKER_TEST_CONFIG')
    if not path:
        pytest.skip('Set PY_INTERVIEW_WORKER_TEST_CONFIG to the private test-worker.json')
    settings = WorkerSettings.load(path)
    assert settings.mode == 'test' and settings.database.dbname == 'py_interview_test'
    return settings


@pytest.mark.postgres
@pytest.mark.redis
def test_is03_retest_suggestion_cites_its_own_observation(worker_settings, pg_settings):
    _, cases, oracles, _ = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS03']
    oracle = oracles['IS03']
    result = execute_session(case, worker_settings, pg_settings[1], ROOT)
    attempt = SessionAttempt(case_id='IS03', **result)
    assert attempt.status == 'completed', attempt.error_code
    assert attempt.model_http_calls == 0
    graded = grade_session(attempt, oracle, case)
    assert graded['machine_pass'] is True, graded['checks']

    output = attempt.output
    assert output['protocol'][CHECK] is True
    probe = {o['id'] for o in output['observations'] if o['label'] == 'needs_probe'}
    assert probe, 'IS03 must record a needs_probe observation'
    suggestions = output['report']['retest']
    assert suggestions, 'the weak branch must resolve at least one retest suggestion'
    for item in suggestions:
        assert item['observation_id'] in probe
        assert item['question'].strip()
        assert item['pack_version']
    # The authored text is a new scenario for the same topic, not the question just answered.
    answered = {t['question_text'] for t in output['turns'] if t.get('question_text')}
    for item in suggestions:
        assert item['question'] not in answered


@pytest.mark.postgres
@pytest.mark.redis
def test_is11_material_plus_wrong_technical_offers_a_retest(worker_settings, pg_settings):
    """The plan's headline sample: real material, a deliberately wrong technical answer."""
    _, cases, oracles, _ = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS11']
    oracle = oracles['IS11']
    result = execute_session(case, worker_settings, pg_settings[1], ROOT)
    attempt = SessionAttempt(case_id='IS11', **result)
    assert attempt.status == 'completed', attempt.error_code
    assert attempt.model_http_calls == 0
    graded = grade_session(attempt, oracle, case)
    assert graded['machine_pass'] is True, graded['checks']

    output = attempt.output
    assert output['protocol'][CHECK] is True
    assert output['protocol']['project_fact_in_scope'] is True
    assert output['protocol']['question_text_quotes_fact'] is True
    assert output['protocol']['application_progress_unchanged'] is True
    probe = {o['id'] for o in output['observations'] if o['label'] == 'needs_probe'}
    suggestions = output['report']['retest']
    assert suggestions, 'the wrong technical answer must yield a suggestion'
    assert suggestions[0]['observation_id'] in probe
    assert suggestions[0]['question'].strip()


@pytest.mark.postgres
@pytest.mark.redis
def test_is12_four_rounds_keep_one_drill_per_topic(worker_settings, pg_settings):
    """Multi-round project run: the probe follows the base, and the review does not repeat a topic."""
    _, cases, oracles, _ = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS12']
    oracle = oracles['IS12']
    result = execute_session(case, worker_settings, pg_settings[1], ROOT)
    attempt = SessionAttempt(case_id='IS12', **result)
    assert attempt.status == 'completed', attempt.error_code
    assert attempt.model_http_calls == 0
    graded = grade_session(attempt, oracle, case)
    assert graded['machine_pass'] is True, graded['checks']

    output = attempt.output
    turns = output['turns']
    assert len(turns) == 4
    assert [t['kind'] for t in turns] == ['project', 'base', 'probe', 'base']
    assert turns[1]['topic_id'] == turns[2]['topic_id'], 'the probe must stay on the same topic'
    assert output['protocol'][CHECK] is True
    suggestions = output['report']['retest']
    topics = [item['topic_id'] for item in suggestions]
    assert len(topics) == len(set(topics)), f'one drill per topic, got {topics}'
    assert 'java-concurrency' in topics


@pytest.mark.postgres
@pytest.mark.redis
def test_is13_retracted_observation_source_is_gone(worker_settings, pg_settings):
    """The session retracts its first observation; the review must not drill that observation."""
    _, cases, oracles, baseline = load_session_suite(SUITE)
    assert 'retracted_observation_not_suggested' in baseline['protocol_checks']
    case = {c.case_id: c for c in cases}['IS13']
    oracle = oracles['IS13']
    result = execute_session(case, worker_settings, pg_settings[1], ROOT)
    attempt = SessionAttempt(case_id='IS13', **result)
    assert attempt.status == 'completed', attempt.error_code
    graded = grade_session(attempt, oracle, case)
    assert graded['machine_pass'] is True, graded['checks']

    output = attempt.output
    corrected = [o for o in output['observations'] if o.get('correction')]
    assert corrected, 'the retraction must be visible in the session view'
    retracted = str(corrected[0]['id'])
    assert output['protocol']['retracted_observation_not_suggested'] is True
    assert all(str(item['observation_id']) != retracted for item in output['report']['retest']), \
        'the withdrawn observation must not be the source of a drill'
