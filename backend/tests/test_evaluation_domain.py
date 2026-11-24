import json
from pathlib import Path
import shutil
import pytest
from interview_agent.evaluation.contracts import Attempt, Task
from interview_agent.evaluation.domain_contracts import DomainAttempt
from interview_agent.evaluation.domain_suite import load_domain_suite, grade_domain, category
from interview_agent.evaluation.suite import load_suite
from interview_agent.evaluation.reviews import item_hash, export_reviews, validate_reviews
from interview_agent.evaluation.output_reviews import export_outputs, validate_outputs

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / 'benchmarks/job-domain-v0.1'


def test_linked_tasks_keep_old_canonical_hashes():
    _, old, old_labels = load_suite(ROOT / 'benchmarks/career-pilot-v0.2')
    manifest, tasks, labels = load_domain_suite(SUITE)
    assert len(tasks) == 54 and manifest['splits'] == {'development': 40, 'reserved_candidate': 14}
    assert len({category(t) for t in tasks}) == 6
    for prior, current in zip(old, tasks):
        assert isinstance(current, Task)
        assert item_hash(prior.model_dump(mode='json')) == item_hash(current.model_dump(mode='json'))
        assert item_hash(old_labels[prior.id].model_dump(mode='json')) == item_hash(labels[current.id].model_dump(mode='json'))


def test_linked_suite_and_new_files_are_bound(tmp_path):
    for name in ('job-domain-v0.1', 'career-pilot-v0.2'):
        shutil.copytree(ROOT / 'benchmarks' / name, tmp_path / name)
    target = tmp_path / 'career-pilot-v0.2/manifest.json'
    target.write_text(target.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='linked_suite_changed'):
        load_domain_suite(tmp_path / 'job-domain-v0.1')


def fixture_attempt():
    _, _, labels = load_domain_suite(SUITE)
    oracle = labels['D01']
    attempt = DomainAttempt(task_id='D01', adapter='system_scripted', repeat=1, status='completed',
        output={'sql_committed': True, 'observed': dict(oracle.expected),
                'guards': dict.fromkeys(oracle.required_guards, True)})
    return attempt, oracle


@pytest.mark.parametrize('mutation', ['missing_null', 'wrong_fact_count', 'missing_guard', 'failed_status'])
def test_business_grade_rejects_missing_or_wrong_evidence(mutation):
    attempt, oracle = fixture_attempt()
    assert grade_domain(attempt, oracle)['machine_pass'] is True
    if mutation == 'missing_null':
        oracle = oracle.model_copy(update={'expected': {'absent_field': None}})
    elif mutation == 'wrong_fact_count':
        attempt.output['observed']['facts'] = 0
    elif mutation == 'missing_guard':
        del attempt.output['guards'][oracle.required_guards[0]]
    else:
        attempt = attempt.model_copy(update={'status': 'product_failure'})
    result = grade_domain(attempt, oracle)
    assert result['machine_pass'] is False and result['domain_success'] is None


def test_infrastructure_failure_is_not_semantic_failure_or_success():
    attempt, oracle = fixture_attempt()
    attempt = attempt.model_copy(update={'status': 'infrastructure_failure'})
    assert grade_domain(attempt, oracle)['machine_pass'] is None
    with pytest.raises(ValueError, match='oracle_task_mismatch'):
        grade_domain(attempt.model_copy(update={'task_id': 'D02'}), oracle)
    with pytest.raises(ValueError):
        Attempt.model_validate(attempt.model_dump())  # Old entry must keep rejecting new task IDs.


def test_new_task_and_output_review_bindings(tmp_path):
    manifest, tasks, labels = load_domain_suite(SUITE)
    selected = [t for t in tasks if t.id == 'D01']
    review = tmp_path / 'tasks'
    export_reviews(review, manifest, selected, labels)
    assert validate_reviews(review / 'reviews.jsonl', manifest, tasks, labels, selected_ids=['D01'])['decisions']['pending'] == 1
    run = tmp_path / 'run'
    run.mkdir()
    attempt, oracle = fixture_attempt()
    row = {'attempt': attempt.model_dump(mode='json'), 'grade': grade_domain(attempt, oracle)}
    (run / 'attempts.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
    (run / 'run.json').write_text(json.dumps({'suite': manifest, 'selected_ids': ['D01'], 'adapters': ['system_scripted'], 'repeats': 1}), encoding='utf-8')
    (run / 'report.json').write_text('{}', encoding='utf-8')
    bundle = tmp_path / 'outputs'
    assert export_outputs(bundle, run, manifest, tasks, labels, attempt_contract=DomainAttempt)['pending'] == 1
    result = validate_outputs(bundle, run, manifest, tasks, labels, attempt_contract=DomainAttempt)
    assert result['decisions']['pending'] == 1 and result['domain_success_rate'] is None
    row['attempt']['output']['observed']['facts'] = 0
    (run / 'attempts.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='output_review_sources_changed'):
        validate_outputs(bundle, run, manifest, tasks, labels, attempt_contract=DomainAttempt)


def test_reserved_candidates_cannot_be_selected(capsys):
    from interview_agent.evaluation.domain import main
    with pytest.raises(SystemExit):
        main(['run', '--root', str(ROOT), '--tasks', 'R05'])
    assert 'development task IDs required' in capsys.readouterr().err


def test_domain_producer_rejects_development_before_services():
    from types import SimpleNamespace
    from interview_agent.evaluation.domain_adapters import execute_domain
    from interview_agent.evaluation.domain_contracts import DomainInput
    public = DomainInput(workflow='materials', message='测试')
    cfg = SimpleNamespace(mode='development')
    with pytest.raises(ValueError, match='evaluation_test_environment_required'):
        execute_domain(public, cfg, None, ROOT)
