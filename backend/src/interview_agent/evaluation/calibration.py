"""Compare saved machine checks with scoped operator findings and full reviews.

An intent/action attestation never becomes an approval of every rubric item.
This module reads saved evidence only: no provider, database or credentials.
"""
import json
from pathlib import Path
from typing import Annotated, Literal
from pydantic import Field, model_validator
from interview_agent.contracts.common import Contract
from .contracts import Attempt
from .output_reviews import load_outputs, validate_outputs, OutputReview
from .reviews import item_hash, validate_reviews


class IntentFinding(Contract):
    task_id: str
    adapter: str
    repeat: Annotated[int, Field(ge=1, le=3)]
    attempt_sha256: str
    task_sha256: str
    oracle_sha256: str
    scope: Literal['intent_action_only'] = 'intent_action_only'
    expected_action_valid: Literal[True]
    action_incorrect: Literal[True]
    reviewer: Annotated[str, Field(min_length=1, max_length=80)]
    statement: Annotated[str, Field(min_length=1, max_length=1200)]
    evidence_refs: Annotated[list[str], Field(min_length=1)]
    source: Literal['operator_attested_chat'] = 'operator_attested_chat'

    @model_validator(mode='after')
    def nonblank(self):
        if not self.reviewer.strip() or not self.statement.strip() or any(not x.strip() for x in self.evidence_refs):
            raise ValueError('intent_finding_evidence_required')
        return self


def validate_findings(path, run_dir, manifest, tasks, oracles, *, attempt_contract=Attempt):
    bound, _, _ = load_outputs(run_dir, manifest, tasks, oracles, attempt_contract=attempt_contract)
    lookup = {tuple(x['key']): x for x in bound}
    rows = [IntentFinding.model_validate_json(s) for s in Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]
    keys = [(r.task_id, r.adapter, r.repeat) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate_intent_finding')
    for row, key in zip(rows, keys):
        original = lookup.get(key)
        if original is None:
            raise ValueError('intent_finding_scope_changed')
        if any(getattr(row, k) != original[k] for k in ('attempt_sha256', 'task_sha256', 'oracle_sha256')):
            raise ValueError('intent_finding_content_changed')
    return rows


def validate_live_intent_scope(path, manifest, tasks, oracles):
    """Bind a prior scoped attestation to the same live public input/oracle.

    Caller first validates against the original saved run. This check does not
    promote an attestation to a full task/output approval.
    """
    rows = [IntentFinding.model_validate_json(s) for s in Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]
    expected = {'G04', 'G09', 'G10'}
    if len(rows) != 3 or {r.task_id for r in rows} != expected:
        raise ValueError('calibration_three_intent_findings_required')
    by_id = {t.id: t for t in tasks}
    for row in rows:
        if row.task_sha256 != item_hash(by_id[row.task_id].model_dump(mode='json')) or row.oracle_sha256 != item_hash(oracles[row.task_id].model_dump(mode='json')):
            raise ValueError('calibration_live_input_changed')
    return {'scoped_intent_action_findings': 3, 'scope': 'intent_action_only',
            'full_task_approval': False, 'full_output_review': False, 'human_identity_verified': False}


def report_calibration(run_dir, task_reviews, output_bundle, manifest, tasks, oracles, *, findings=None, attempt_contract=Attempt):
    bound, _, sources = load_outputs(run_dir, manifest, tasks, oracles, attempt_contract=attempt_contract)
    ids = sorted({x['attempt'].task_id for x in bound})
    task_summary = validate_reviews(task_reviews, manifest, tasks, oracles, selected_ids=ids)
    output_summary = validate_outputs(output_bundle, run_dir, manifest, tasks, oracles, attempt_contract=attempt_contract)
    task_rows = {r['task_id']: r for r in [json.loads(s) for s in Path(task_reviews).read_text(encoding='utf-8').splitlines() if s.strip()]}
    mapping = json.loads((Path(output_bundle) / 'mapping.json').read_text(encoding='utf-8'))
    review_rows = {r.sample_id: r for r in [OutputReview.model_validate_json(s) for s in (Path(output_bundle) / 'reviews.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]}
    saved = [json.loads(s) for s in (Path(run_dir) / 'attempts.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    raw = {(r['attempt']['task_id'], r['attempt']['adapter'], r['attempt']['repeat']): r for r in saved}
    scoped = validate_findings(findings, run_dir, manifest, tasks, oracles, attempt_contract=attempt_contract) if findings else []
    scoped_keys = {(r.task_id, r.adapter, r.repeat) for r in scoped}
    samples = []
    counts = {'both_pass': 0, 'both_fail': 0, 'machine_pass_human_fail': 0, 'machine_fail_human_pass': 0}
    for item in mapping['samples']:
        key = tuple(item['key'])
        review = review_rows[item['sample_id']]
        machine = raw[key]['grade']['machine_pass']
        if machine is not None and type(machine) is not bool:
            raise ValueError('calibration_machine_grade_invalid')
        task_decision = task_rows.get(key[0], {}).get('decision', 'missing')
        eligible = task_decision == 'approve' and review.decision in ('pass', 'fail') and machine is not None
        disagreement = None
        if eligible:
            human = review.decision == 'pass'
            name = ('both_pass' if human else 'machine_pass_human_fail') if machine else ('machine_fail_human_pass' if human else 'both_fail')
            counts[name] += 1
            disagreement = machine != human
        samples.append({'sample_id': item['sample_id'], 'key': list(key), 'machine_pass': machine,
            'task_review': task_decision, 'output_review': review.decision, 'comparison_eligible': eligible,
            'machine_human_disagreement': disagreement, 'scoped_intent_failure_attested': key in scoped_keys})
    compared = sum(counts.values())
    return {'scope': 'saved_evidence_calibration_not_new_model_evaluation', 'sources': sources,
        'task_review': task_summary, 'output_review': output_summary, 'samples': samples,
        'full_review_comparisons': compared, 'comparison_counts': counts,
        'disagreement_rate_eligible': (counts['machine_pass_human_fail'] + counts['machine_fail_human_pass']) / compared if compared else None,
        'scoped_intent_action_findings': len(scoped), 'scoped_findings_are_full_reviews': False,
        'human_identity_verified': False, 'domain_success_rate': None, 'new_provider_http_calls': 0}


def main(argv=None):
    import argparse
    from uuid import uuid4
    from .suite import load_suite
    from .domain_suite import load_domain_suite
    from .domain_contracts import DomainAttempt
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--suite-kind', choices=['pilot', 'domain'], default='domain')
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--task-reviews', type=Path, required=True)
    parser.add_argument('--output-bundle', type=Path, required=True)
    parser.add_argument('--intent-findings', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    loader, attempt = (load_domain_suite, DomainAttempt) if args.suite_kind == 'domain' else (load_suite, Attempt)
    directory = args.suite or root / ('benchmarks/job-domain-v0.1' if args.suite_kind == 'domain' else 'benchmarks/career-pilot-v0.2')
    manifest, tasks, oracles = loader(directory)
    result = report_calibration(args.run_dir, args.task_reviews, args.output_bundle, manifest, tasks, oracles,
                                findings=args.intent_findings, attempt_contract=attempt)
    out = root / '.private/ja-09-05/calibration' / uuid4().hex
    out.mkdir(parents=True)
    (out / 'report.json').write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode())
    print(json.dumps({'output': str(out), 'full_review_comparisons': result['full_review_comparisons'],
        'scoped_intent_action_findings': result['scoped_intent_action_findings'], 'domain_success_rate': None}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
