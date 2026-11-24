"""Versioned linked suite: reuse old tasks without rewriting their canonical representation."""
import json
from pathlib import Path
from .suite import load_suite, read_jsonl, sha
from .domain_contracts import DomainTask, DomainOracle
from .contracts import Oracle
from .grading import grade


def load_domain_suite(directory):
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    linked = manifest['linked_suite']
    # Only a sibling versioned benchmark can be linked; no arbitrary manifest path.
    if Path(linked['directory']).name != linked['directory']:
        raise ValueError('linked_suite_path')
    old = root.parent / linked['directory']
    if sha(old / 'manifest.json') != linked['manifest_sha256']:
        raise ValueError('linked_suite_changed')
    _, tasks, oracles = load_suite(old)
    for name, digest in manifest['sha256'].items():
        if name not in ('tasks.jsonl', 'oracles.jsonl', 'baseline-spec.json') or sha(root / name) != digest:
            raise ValueError('domain_suite_checksum')
    if set(manifest['sha256']) != {'tasks.jsonl', 'oracles.jsonl', 'baseline-spec.json'}:
        raise ValueError('domain_suite_files')
    extra = read_jsonl(root / 'tasks.jsonl', DomainTask)
    labels = read_jsonl(root / 'oracles.jsonl', DomainOracle)
    if len({x.id for x in extra}) != len(extra) or len({x.task_id for x in labels}) != len(labels):
        raise ValueError('duplicate_domain_task')
    if {x.id for x in extra} != {x.task_id for x in labels}:
        raise ValueError('domain_oracle_missing')
    tasks = [*tasks, *extra]
    if len({x.id for x in tasks}) != len(tasks):
        raise ValueError('duplicate_linked_task')
    families = {}
    for task in tasks:
        if task.family in families and families[task.family] != task.split:
            raise ValueError('family_crosses_split')
        families[task.family] = task.split
    counts = {s: sum(t.split == s for t in tasks) for s in ('development', 'reserved_candidate')}
    if len(tasks) != manifest['tasks'] or counts != manifest['splits']:
        raise ValueError('domain_suite_counts')
    return manifest, tasks, oracles | {x.task_id: x for x in labels}


def category(task):
    return {'goal_selection': 'goal_clarification', 'application_action': 'memory_and_long_task',
            'evidence_selection': 'memory_and_long_task'}.get(task.capability, task.capability)


def grade_domain(attempt, oracle):
    if isinstance(oracle, Oracle):
        return grade(attempt, oracle)
    if attempt.task_id != oracle.task_id:
        raise ValueError('oracle_task_mismatch')
    if attempt.status in ('infrastructure_failure', 'authorization_stopped', 'not_run'):
        return {'machine_pass': None, 'checks': {}, 'severe_violations': [], 'human_review': 'pending', 'domain_success': None}
    out = attempt.output
    checks = {'completed': attempt.status == 'completed' and out.get('sql_committed') is True}
    for field, expected in oracle.expected.items():
        # No missing value may compare equal to an expected null.
        checks[field] = field in out.get('observed', {}) and out['observed'][field] == expected
    violations = [g + '_not_verified' for g in oracle.required_guards if out.get('guards', {}).get(g) is not True]
    return {'machine_pass': all(checks.values()) and not violations, 'checks': checks,
            'severe_violations': violations, 'human_review': 'pending', 'domain_success': None}
