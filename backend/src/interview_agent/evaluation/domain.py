"""Six-category free protocol pilot. No live provider or authorization command."""
import argparse
import json
from pathlib import Path
from uuid import uuid4
from .__main__ import write, provenance, ledger
from .adapters import arun, execute_input
from .contracts import Task
from .domain_contracts import DomainAttempt
from .domain_suite import load_domain_suite, grade_domain, category
from .grading import summarize


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'run', 'review-export', 'review-validate', 'output-review-export', 'output-review-validate'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--tasks', nargs='+')
    parser.add_argument('--worker-config', type=Path)
    parser.add_argument('--owner-config', type=Path)
    parser.add_argument('--executor-profile', type=Path)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--review-file', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest, tasks, oracles = load_domain_suite(args.suite or root / 'benchmarks/job-domain-v0.1')
    selected = [t for t in tasks if t.split == 'development']
    if args.tasks:
        if len(args.tasks) != len(set(args.tasks)) or not set(args.tasks) <= {t.id for t in selected}:
            parser.error('unique development task IDs required; reserved candidates are not executable by this entry')
        selected = [t for t in selected if t.id in args.tasks]
    private = root / '.private/ja-09-04'
    if args.command == 'validate':
        print(json.dumps({'suite': manifest['version'], 'tasks': len(tasks), 'splits': manifest['splits'],
            'categories': {c: sum(category(t) == c for t in tasks) for c in sorted({category(t) for t in tasks})},
            'human_review': 'pending', 'model_http_calls': 0}, ensure_ascii=False))
        return 0
    if args.command.startswith('output-review-'):
        from .output_reviews import export_outputs, validate_outputs
        if not args.run_dir:
            parser.error('saved --run-dir required')
        if args.command == 'output-review-export':
            out = private / 'output-reviews' / uuid4().hex
            result = export_outputs(out, args.run_dir, manifest, tasks, oracles, attempt_contract=DomainAttempt)
            result['output'] = str(out)
        else:
            if not args.bundle:
                parser.error('--bundle required')
            result = validate_outputs(args.bundle, args.run_dir, manifest, tasks, oracles, attempt_contract=DomainAttempt)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command.startswith('review-'):
        from .reviews import export_reviews, validate_reviews
        if args.command == 'review-export':
            out = private / 'reviews' / uuid4().hex
            result = export_reviews(out, manifest, selected, oracles)
            result['output'] = str(out)
        else:
            if not args.review_file:
                parser.error('--review-file required')
            result = validate_reviews(args.review_file, manifest, tasks, oracles, selected_ids=[t.id for t in selected])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if not args.worker_config or not args.owner_config:
        parser.error('run requires explicit test worker/owner configs')
    from interview_agent.worker.config import WorkerSettings
    from interview_agent.storage.config import DatabaseSettings
    from interview_agent.execution.docker_java import DockerProfile
    from .domain_adapters import execute_domain
    settings = WorkerSettings.load(args.worker_config)
    owner = DatabaseSettings.model_validate_json(args.owner_config.read_text(encoding='utf-8'))
    if settings.mode != 'test' or owner.dbname != 'py_interview_test' or (owner.host, owner.port) != (settings.database.host, settings.database.port):
        parser.error('matching test environment required')
    requires_executor = any(not isinstance(t, Task) and t.input.workflow in ('coding', 'written') for t in selected)
    if requires_executor and not args.executor_profile:
        parser.error('execution tasks require --executor-profile; no fallback grader')
    profile = DockerProfile.load(args.executor_profile) if args.executor_profile else None
    if requires_executor:
        from interview_agent.execution.docker_java import DockerJava
        arun(DockerJava(profile).readiness())
    run_dir = private / 'benchmark' / uuid4().hex
    run_dir.mkdir(parents=True)
    ids = [t.id for t in selected]
    meta = {'suite': manifest, 'selected_ids': ids, 'adapters': ['system_scripted'], 'repeats': 1,
        'provenance': provenance(root), 'reserved_executed': False, 'human_review': 'pending',
        'provider': 'scripted_protocol', 'source_transport': 'synthetic_http_replay', 'ledger_before': arun(ledger(owner))}
    write(run_dir / 'run.json', meta)
    records = []
    for task in selected:
        if isinstance(task, Task):
            result = execute_input(task.input, 'system_scripted', settings, owner)
        else:
            result = execute_domain(task.input, settings, owner, root, profile)
        attempt = DomainAttempt(task_id=task.id, adapter='system_scripted', repeat=1, **result)
        row = {'attempt': attempt.model_dump(mode='json'), 'grade': grade_domain(attempt, oracles[task.id])}
        records.append(row)
        with (run_dir / 'attempts.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        print(json.dumps({'task_id': task.id, 'status': attempt.status, 'machine_pass': row['grade']['machine_pass']}, ensure_ascii=False), flush=True)
    after = arun(ledger(owner))
    report = summarize(records, ids, ['system_scripted'], 1)
    by_id = {t.id: t for t in selected}
    report['categories'] = {c: {'attempts': sum(category(by_id[x['attempt']['task_id']]) == c for x in records),
        'machine_passes': sum(x['grade']['machine_pass'] is True and category(by_id[x['attempt']['task_id']]) == c for x in records)} for c in sorted({category(t) for t in selected})}
    report.update(ledger_unchanged=meta['ledger_before'] == after, human_task_approval=0,
        human_output_approval=0, reserved_executed=False, evaluation_scope='free_business_protocol_not_llm_quality')
    report['task_independence'] = 'task IDs counted; shared templates/catalogs/repos, not statistically independent samples'
    write(run_dir / 'report.json', report)
    print(json.dumps({'output': str(run_dir), **report}, ensure_ascii=False))
    return 0 if report['ledger_unchanged'] and not report['metrics']['system_scripted']['infrastructure_failures'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
