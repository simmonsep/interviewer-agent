"""S00E: the whole-session suite, candidate contract and grading layers.

Unit tests stay offline. The integration test uses the real test PostgreSQL/Redis,
the real FastAPI app and the real scripted Worker; it makes no provider call.
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from interview_agent.evaluation.interview_session import (
    SessionAttempt, execute_session, grade_session,
    load_session_suite, main, resolve_rule, summarize_session)
from interview_agent.evaluation.reviews import Review, item_hash
from interview_agent.worker.config import WorkerSettings

from test_postgres import pg_settings  # noqa: F401  (pytest fixture reuse)

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / 'benchmarks/interview-session-v0.1'


def copy_suite(tmp_path):
    target = tmp_path / 'interview-session-v0.1'
    shutil.copytree(SUITE, target)
    return target


def rewrite_case(root, case_id, **changes):
    path = root / 'cases.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    for row in rows:
        if row['case_id'] == case_id:
            row.update(changes)
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')) for r in rows) + '\n', encoding='utf-8')
    import hashlib
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    manifest['sha256']['cases.jsonl'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def test_suite_is_frozen_and_balanced():
    manifest, cases, oracles, baseline = load_session_suite(SUITE)
    assert manifest['version'] == 'interview-session-v0.1'
    assert manifest['cases'] == 10
    assert manifest['splits'] == {'development': 8, 'reserved_candidate': 2}
    assert len(cases) == len(oracles) == 10
    assert baseline['provider'] == 'scripted' and baseline['semantic_scoring_eligible'] is False
    assert all(c.provenance == 'project_authored_synthetic' for c in cases)
    assert all(o.human_rubric for o in oracles.values())


def test_checksum_mismatch_is_rejected(tmp_path):
    root = copy_suite(tmp_path)
    target = root / 'cases.jsonl'
    target.write_text(target.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='session_suite_checksum'):
        load_session_suite(root)


def test_pair_mismatch_is_rejected(tmp_path):
    root = copy_suite(tmp_path)
    rewrite_case(root, 'IS05', mode='coaching')
    with pytest.raises(ValueError, match='session_pair_mismatch'):
        load_session_suite(root)


def test_family_crossing_split_is_rejected(tmp_path):
    root = copy_suite(tmp_path)
    rewrite_case(root, 'IS09', family_id='branch-vague-probe')
    with pytest.raises(ValueError, match='session_family_crosses_split'):
        load_session_suite(root)


def test_oracle_cannot_require_an_undeclared_check(tmp_path):
    root = copy_suite(tmp_path)
    path = root / 'oracles.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    rows[0]['protocol_checks'] = [*rows[0]['protocol_checks'], 'invented_check']
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')) for r in rows) + '\n', encoding='utf-8')
    import hashlib
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    manifest['sha256']['oracles.jsonl'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='session_oracle_check_unknown'):
        load_session_suite(root)


def test_candidate_prefers_question_id_then_topic_and_never_invents():
    _, cases, _, _ = load_session_suite(SUITE)
    indexed = {c.case_id: c for c in cases}
    # Exact question ID wins over everything else.
    exact = resolve_rule(indexed['IS02'], {'question_id': 'java-concurrency-base',
                                           'topic_id': 'java-concurrency', 'kind': 'base'})
    assert exact[0].branch == 'vague' and exact[1] is None
    # A case that only has an exact-ID rule must not silently reuse it for another question:
    # the honest answer is that the fixture does not cover it.
    assert resolve_rule(indexed['IS02'], {'question_id': 'invented-id', 'topic_id': 'java-concurrency',
                                          'kind': 'probe'}) == (None, 'fixture_uncovered')
    # topic+kind is the second rung; IS04/IS05 author rules at that level on purpose.
    by_topic = resolve_rule(indexed['IS04'], {'question_id': 'invented-id', 'topic_id': 'java-concurrency',
                                              'kind': 'probe'})
    assert by_topic[0].branch == 'substantive' and by_topic[1] is None
    assert resolve_rule(indexed['IS04'], {'question_id': 'redis-lock-base', 'topic_id': 'redis-lock',
                                          'kind': 'base'}) == (None, 'fixture_uncovered')
    # A question outside the declared case scope is a different, non-hiding signal.
    assert resolve_rule(indexed['IS02'], {'question_id': 'other', 'topic_id': 'agent-tools',
                                          'kind': 'base'}) == (None, 'agent_out_of_scope')


def test_reserved_candidates_cannot_be_selected(capsys):
    with pytest.raises(SystemExit):
        main(['run', '--root', str(ROOT), '--cases', 'IS09'])
    assert 'reserved candidates are not executable' in capsys.readouterr().err


def _attempt(case_id, status='completed', protocol=None, turns=None, final='COMPLETED',
             stop='question_limit'):
    return SessionAttempt(case_id=case_id, status=status, output={
        'final_status': final, 'stop_reason': stop, 'protocol': protocol or {},
        'turns': turns or [], 'report': {'semantic_scoring_eligible': False}})


def test_grade_rejects_turn_mismatch_and_severe_gap():
    _, cases, oracles, _ = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS01']
    oracle = oracles['IS01']
    good_protocol = {name: True for name in oracle.protocol_checks}
    turns = [{'topic_id': 'java-concurrency', 'kind': 'base', 'question_id': 'java-concurrency-base',
              'assisted': False},
             {'topic_id': 'redis-lock', 'kind': 'base', 'question_id': 'redis-lock-base',
              'assisted': False}]
    graded = grade_session(_attempt('IS01', protocol=good_protocol, turns=turns), oracle, case)
    assert graded['machine_pass'] is False
    assert graded['checks']['expected_turn_sequence'] is False
    turns[1] = {'topic_id': 'sql-isolation', 'kind': 'base', 'question_id': 'sql-isolation-base',
                'assisted': False}
    broken = dict(good_protocol, answers_preserved=False)
    graded = grade_session(_attempt('IS01', protocol=broken, turns=turns), oracle, case)
    assert graded['machine_pass'] is False
    assert 'answers_preserved_not_verified' in graded['severe_violations']


def test_incomplete_attempt_has_no_semantic_rate_and_no_denominator():
    _, cases, oracles, baseline = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS01']
    oracle = oracles['IS01']
    attempt = _attempt('IS01', status='evaluation_incomplete')
    graded = grade_session(attempt, oracle, case)
    assert graded['machine_pass'] is None and graded['domain_success'] is None
    report = summarize_session([{'attempt': attempt.model_dump(mode='json'), 'grade': graded}],
                               ['IS01'], oracles, baseline)
    assert report['metrics']['machine_pass_rate_valid'] is None
    assert report['metrics']['cost_per_success'] is None
    assert report['metrics']['evaluation_incomplete'] == 1
    assert report['protocol_checks']['not_run'] == len(oracle.protocol_checks)
    assert report['semantic']['semantic_pass_rate'] is None


def test_review_export_never_claims_human_approval(tmp_path):
    manifest, cases, oracles, _ = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS01']
    row = Review(task_id=case.case_id, suite_version=manifest['version'],
                 task_sha256=item_hash(case.model_dump(mode='json')),
                 oracle_sha256=item_hash(oracles['IS01'].model_dump(mode='json')))
    assert row.decision == 'pending' and row.source == 'operator_attested'
    approved = row.model_dump()
    approved['decision'] = 'approve'
    with pytest.raises(ValueError, match='reviewer_and_reason_required'):
        Review.model_validate(approved)
    changed = row.model_dump()
    changed['task_sha256'] = '0' * 64
    assert Review.model_validate(changed).task_sha256 != item_hash(case.model_dump(mode='json'))


@pytest.fixture(scope='session')
def worker_settings(pg_settings):
    path = os.environ.get('PY_INTERVIEW_WORKER_TEST_CONFIG')
    if not path:
        pytest.skip('Set PY_INTERVIEW_WORKER_TEST_CONFIG to the private test-worker.json')
    settings = WorkerSettings.load(path)
    assert settings.mode == 'test' and settings.database.dbname == 'py_interview_test'
    return settings


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.parametrize('case_id', ['IS01', 'IS04', 'IS05', 'IS07', 'IS08'])
def test_scripted_session_protocol_end_to_end(case_id, worker_settings, pg_settings):
    """Real API + Worker + SQL + Redis; scripted provider; no provider HTTP."""
    _, cases, oracles, _ = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}[case_id]
    oracle = oracles[case_id]
    result = execute_session(case, worker_settings, pg_settings[1], ROOT)
    attempt = SessionAttempt(case_id=case_id, **result)
    graded = grade_session(attempt, oracle, case)
    assert attempt.status == 'completed', attempt.error_code
    assert attempt.model_http_calls == 0
    assert graded['machine_pass'] is True, graded['checks']
    output = attempt.output
    assert [t['question_id'] for t in output['turns']] == [t.question_id for t in oracle.expected_turns]
    assert output['report']['semantic_scoring_eligible'] is False
    assert len({o['answer_id'] for o in output['observations']}) == len(output['turns'])
    if case.mode == 'interview':
        assert output['protocol']['formal_mode_no_explanation'] is True
    else:
        assert output['protocol']['coaching_explanation_present'] is True
    if case.source_text:
        assert output['protocol']['project_fact_in_scope'] is True
        assert output['protocol']['question_text_quotes_fact'] is True


@pytest.mark.postgres
@pytest.mark.redis
def test_uncovered_question_is_reported_not_answered(worker_settings, pg_settings):
    """A question the fixture has no rule for must stop the run honestly.

    The point is that the candidate layer never improvises an answer: the attempt is
    `evaluation_incomplete`, classified as stage=interview / reason=fixture_uncovered,
    and it does not enter the pass-rate denominator.
    """
    _, cases, oracles, baseline = load_session_suite(SUITE)
    case = {c.case_id: c for c in cases}['IS01']
    narrowed = case.model_copy(update={'candidate': case.candidate.model_copy(
        update={'rules': [case.candidate.rules[1]]})})  # only the sql-isolation rule
    result = execute_session(narrowed, worker_settings, pg_settings[1], ROOT)
    attempt = SessionAttempt(case_id='IS01', **result)
    assert attempt.status == 'evaluation_incomplete', attempt.error_code
    assert attempt.stage == 'interview'
    assert attempt.error_code == 'candidate_fixture_uncovered'
    assert attempt.output['probes']['unmatched']['reason'] == 'fixture_uncovered'
    assert attempt.output['turns'] == []
    graded = grade_session(attempt, oracles['IS01'], case)
    assert graded['machine_pass'] is None and graded['domain_success'] is None
    report = summarize_session([{'attempt': attempt.model_dump(mode='json'), 'grade': graded}],
                               ['IS01'], oracles, baseline)
    assert report['metrics']['machine_pass_rate_valid'] is None
    assert report['failures'] == [{'case_id': 'IS01', 'status': 'evaluation_incomplete',
                                   'stage': 'interview', 'reason': 'candidate_fixture_uncovered'}]
    assert report['protocol_checks']['not_run'] == len(oracles['IS01'].protocol_checks)
