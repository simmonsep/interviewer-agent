"""F07Q P0 §2.2: fixed observation samples driven through the real interview graph.

What is real here:
  * the same SYSTEM_PROMPT, tool schemas and SQL-side validation rules as production
    (`interviews.agent.interview` plus `contracts.validate_decision`);
  * the same citation gate (answer_quote must be inside the answer, evidence_quote
    must be inside the frozen reference).

What is NOT real here (until an explicitly authorized live run):
  * no model HTTP: `run` uses the product's own scripted adapter, whose lexical rules
    are a protocol demo, so its scores are a BASELINE, not a quality result.

`run-live` refuses to start without an explicit stage authorization, a credentials
file and task IDs, mirroring `evaluation.__main__` rather than inventing a new path.
"""
import argparse, asyncio, hashlib, json
from pathlib import Path
from uuid import UUID, uuid4, uuid5

SUITE = 'benchmarks/interview-observation-v0.1'
NAMESPACE = UUID('6c1f0f18-0f4a-5c1e-9a41-2b2f4f0f0001')
KINDS = ('correct', 'partial', 'incorrect', 'insufficient', 'fabricated_number',
         'ambiguous_contribution', 'concept_confusion', 'evidence_conflict')


def uid(name):
    return str(uuid5(NAMESPACE, name))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_suite(root):
    directory = Path(root) / SUITE
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    for name, digest in manifest['sha256'].items():
        if sha256(directory / name) != digest:
            raise ValueError('observation_suite_checksum_mismatch:' + name)
    samples = [json.loads(line) for line in (directory / 'samples.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    rubric = json.loads((directory / 'rubric.json').read_text(encoding='utf-8'))
    ids = [s['id'] for s in samples]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_sample_id')
    if set(s['kind'] for s in samples) != set(KINDS):
        raise ValueError('sample_kind_coverage_incomplete')
    for sample in samples:
        if sample['kind'] not in KINDS:
            raise ValueError('unknown_sample_kind:' + sample['id'])
        if sample['expected']['label'] not in ('needs_probe', 'supported_in_this_answer', 'inconclusive'):
            raise ValueError('invalid_expected_label:' + sample['id'])
        if not sample['expected']['actions']:
            raise ValueError('missing_allowed_actions:' + sample['id'])
        if sample['topic_id'] == 'project' and not sample.get('fact', {}).get('quote'):
            raise ValueError('project_sample_needs_fact_quote:' + sample['id'])
    return manifest, samples, rubric


def build_scope(sample):
    """Synthetic, frozen scope: the same shape the worker passes into the graph."""
    from interview_agent.interviews.catalog import pack
    role = sample['role']
    pack_value = pack(role)
    topic_id = sample['topic_id']
    answer_id = uid(sample['id'] + ':answer')
    question_row = uid(sample['id'] + ':question')
    facts = []
    if topic_id == 'project':
        fact_id = uid(sample['id'] + ':fact')
        facts = [{'id': fact_id, 'quote': sample['fact']['quote'], 'resume_source': None}]
        question = {'id': question_row, 'topic_id': 'project', 'question_id': 'project-contribution',
                    'text': '材料中你确认了「' + sample['fact']['quote'] + '」。请区分你亲自完成的部分与团队其他人的工作。',
                    'kind': 'project', 'fact_id': fact_id, 'project_focus': 'contribution', 'assisted': False}
    else:
        item = pack_value['topics'][topic_id]
        kind = sample['question_kind']
        question = {'id': question_row, 'topic_id': topic_id, 'question_id': topic_id + '-' + kind,
                    'text': item[kind], 'kind': kind, 'fact_id': None, 'assisted': False}
    return {
        'policy': {'max_questions': 4, 'minimum_questions': 2, 'mode': 'interview',
                   'allow_project_questions': True},
        'facts': facts,
        'current_question': question,
        'answer': {'id': answer_id, 'text': sample['answer']},
        'assisted_topics': [],
        'prior_written_results': [],
        'material_alignment': {},
        'question_banks': [],
        'target': {'title': '固定评测样本（合成）', 'text': '用于 observation 判据回归的合成岗位描述。', 'requirements': ''},
        'question_history': [question],
        'prior_observations': [],
        'pack': pack_value,
        'application': None,
    }


async def drive(scope, provider):
    import asyncio
    from interview_agent.interviews.agent import interview
    cancel = asyncio.Event()
    return await interview(scope, provider, cancel)


def scripted_provider(scope):
    from interview_agent.interviews.agent import ScriptedInterviewer

    async def admit():
        return None

    return ScriptedInterviewer(scope, admit)


def execute(sample, adapter):
    import asyncio
    scope = build_scope(sample)
    if adapter != 'scripted':
        raise ValueError('adapter_not_supported_without_authorization:' + adapter)
    try:
        decision = asyncio.run(drive(scope, scripted_provider(scope)))
        return {'status': 'ok', 'decision': decision.model_dump(mode='json') if decision else None,
                'input': {'answer': sample['answer'], 'question': scope['current_question']['text']}}
    except Exception as error:  # noqa: BLE001 - classified, never re-raised as a quality signal
        code = getattr(error, 'code', None) or str(error)
        trace = getattr(error, 'diagnostic', {}).get('tool_log', [])
        return {'status': 'infrastructure_failure', 'error_code': code, 'tool_log': trace,
                'input': {'answer': sample['answer'], 'question': scope['current_question']['text']}}


def hits(items, text, guards=False):
    """Keyword hits. `guards=True` ignores a phrase that is only being *quoted to reject it*
    ("断言POST天然幂等" must not count as accepting POST idempotency). Coarse, not semantic."""
    cues = ('断言', '认为', '称为', '称', '声称', '宣称', '定义为', '等同', '等于', '不符',
            '推断', '把', '错误', '误解', '不是', '并非', '未', '没有', '否认', '质疑', '混淆')
    found = []
    for item in items:
        hit = False
        for word in item['any_of']:
            start = text.find(word)
            while start >= 0 and not hit:
                window = text[max(0, start - 12):start]
                hit = not (guards and any(cue in window for cue in cues))
                start = text.find(word, start + 1)
            if hit:
                break
        if hit:
            found.append(item['id'])
    return found


def grade(record, sample):
    expected = sample['expected']
    if record['status'] != 'ok' or not record.get('decision'):
        return {'sample_id': sample['id'], 'kind': sample['kind'], 'status': 'not_graded',
                'error_code': record.get('error_code'), 'passed': False}
    decision = record['decision']
    observation = decision.get('observation') or {}
    summary = observation.get('summary') or ''
    label = observation.get('label')
    action = decision.get('action')
    check_sources = {'summary': summary}
    missed = [check['id'] for check in expected['checks'] if not hits([check], check_sources[check['in']])]
    forbidden = [item['id'] for item in expected['forbid'] if hits([item], summary, guards=True)]
    label_ok = label == expected['label']
    action_ok = (action in expected['actions']) if action is not None else None
    return {'sample_id': sample['id'], 'kind': sample['kind'], 'status': 'graded',
            'label': label, 'expected_label': expected['label'], 'label_ok': label_ok,
            'action': action, 'action_ok': action_ok,
            'summary': summary, 'missed_checks': missed, 'forbidden_hits': forbidden,
            'passed': bool(label_ok and action_ok is not False and not missed and not forbidden)}


def summarize(records):
    graded = [r['grade'] for r in records if r['grade']['status'] == 'graded']
    failures = [r['grade'] for r in records if r['grade']['status'] != 'graded']
    by_kind = {}
    for item in graded + failures:
        bucket = by_kind.setdefault(item['kind'], {'samples': 0, 'passed': 0, 'label_ok': 0})
        bucket['samples'] += 1
        bucket['passed'] += int(bool(item.get('passed')))
        bucket['label_ok'] += int(bool(item.get('label_ok')))
    return {
        'samples': len(records),
        'executed': len(graded),
        'infrastructure_failures': len(failures),
        'labels_correct': sum(int(bool(r.get('label_ok'))) for r in graded),
        'label_accuracy': round(sum(int(bool(r.get('label_ok'))) for r in graded) / len(graded), 4) if graded else None,
        'passed': sum(int(bool(r.get('passed'))) for r in graded),
        'pass_rate': round(sum(int(bool(r.get('passed'))) for r in graded) / len(graded), 4) if graded else None,
        'forbid_violations': sorted({hit for r in graded for hit in r['forbidden_hits']}),
        'missed_checks': sorted({hit for r in graded for hit in r['missed_checks']}),
        'by_kind': by_kind,
    }


def provenance(root):
    import subprocess, sys
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, check=False).stdout.decode().strip()
    except OSError:
        head = None
    sources = sorted((Path(root) / 'backend/src').rglob('*.py'))
    return {'git_commit': head or None, 'python': sys.version,
            'source_sha256': {p.relative_to(root).as_posix(): sha256(p) for p in sources if p.exists()},
            'no_model_private_thoughts_recorded': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'run', 'run-live'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--adapter', default='scripted', choices=['scripted', 'live'])
    parser.add_argument('--samples', nargs='+')
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--authorization')
    parser.add_argument('--max-cny')
    parser.add_argument('--api-config', type=Path)
    parser.add_argument('--worker-config', type=Path)
    parser.add_argument('--owner-config', type=Path)
    parser.add_argument('--repeats', type=int, choices=[1, 2], default=1)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest, samples, rubric = load_suite(root)
    if args.samples:
        if not set(args.samples) <= {s['id'] for s in samples}:
            parser.error('unknown sample id')
        samples = [s for s in samples if s['id'] in set(args.samples)]
    if args.command == 'validate':
        print(json.dumps({'status': 'valid', 'suite': manifest['version'], 'samples': len(samples),
                          'kinds': sorted({s['kind'] for s in samples}), 'rubric': rubric['version'],
                          'model_http_calls': 0, 'human_review': 'pending'}, ensure_ascii=False))
        return 0
    if args.command == 'run-live':
        # The only path in this repo that spends the operator's money. It fails closed:
        # explicit budget string, explicit configs, explicit credentials, fixed stage id.
        if not all([args.api_config, args.worker_config, args.owner_config, args.policy,
                    args.credentials, args.max_cny]):
            parser.error('run-live requires --api-config --worker-config --owner-config --policy '
                         '--credentials --max-cny; it never authorizes automatically')
        from .observation_live import execute as execute_live, MAX_CNY, AUTHORIZATION_ID
        if args.max_cny != MAX_CNY:
            parser.error('run-live budget must equal the approved %s CNY stage' % MAX_CNY)
        if args.authorization and args.authorization != AUTHORIZATION_ID:
            parser.error('authorization id must be ' + AUTHORIZATION_ID)
        out, report, _ = asyncio.run(execute_live(
            root, samples, args.repeats,
            api_config=args.api_config, worker_config=args.worker_config,
            owner_config=args.owner_config, policy_path=args.policy,
            credentials_path=args.credentials,
            output_dir=root / '.private/f07q/observation-live'),
            loop_factory=__import__('interview_agent.api.__main__', fromlist=['selector_loop']).selector_loop)
        print(json.dumps({'status': 'recorded', 'output': str(out), 'metrics': report}, ensure_ascii=False))
        return 0
    out = root / '.private/f07q/observation' / uuid4().hex
    out.mkdir(parents=True)
    write(out / 'run.json', {'suite': manifest, 'rubric': rubric['version'], 'adapter': args.adapter,
                             'selected': [s['id'] for s in samples], 'provenance': provenance(root),
                             'model_http_calls': 0,
                             'scope': 'real interview LangGraph + real validation; scripted adapter only'})
    records = []
    with (out / 'attempts.jsonl').open('x', encoding='utf-8', newline='\n') as handle:
        for sample in samples:
            result = execute(sample, args.adapter)
            record = {'sample_id': sample['id'], 'kind': sample['kind'], 'result': result,
                      'grade': grade(result, sample)}
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
            handle.flush()
    report = summarize(records)
    report.update(suite=manifest['version'], adapter=args.adapter, human_review='pending',
                  semantic_result_valid=False,
                  note='scripted adapter is a protocol demo; these scores are a baseline, not model quality')
    write(out / 'report.json', report)
    print(json.dumps({'status': 'recorded', 'output': str(out), 'metrics': report}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
