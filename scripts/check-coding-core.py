"""Local controlled JA-07-01 demonstration; scripted fixture, zero model HTTP calls."""
import argparse
import asyncio
import json
import runpy
from pathlib import Path
from uuid import uuid4
from interview_agent.coding.catalog import task, TASKS
from interview_agent.coding.agent import assist, final_grade, ScriptedCodingDemo
from interview_agent.execution.java_workspace import JavaWorkspaceExecutor
from interview_agent.execution.docker_java import DockerJava, DockerProfile

ROOT = Path(__file__).resolve().parents[1]


async def check(profile, identities, output):
    output.mkdir(parents=True, exist_ok=False)
    isolation = await runpy.run_path(str(ROOT / 'scripts/check-executor.py'))['check'](profile)
    (output / 'isolation.json').write_text(json.dumps(isolation, ensure_ascii=False, indent=2), encoding='utf-8')
    solutions = json.loads((ROOT / 'fixtures/ja07/scripted-solutions.json').read_text(encoding='utf-8'))
    executor = JavaWorkspaceExecutor(profile); results = []
    for identity in identities:
        value = task(identity); cancelled = asyncio.Event()
        candidate = await assist(value, ScriptedCodingDemo(value, solutions[identity]), executor, cancelled)
        final = await final_grade(value, candidate, executor, cancelled)
        assert candidate['tests'][0]['result']['verdict'] == 'WRONG_ANSWER', 'Starter must fail the actual public tests'
        assert candidate['tests'][-1]['result']['verdict'] == 'ACCEPTED'
        assert final['result']['verdict'] == 'ACCEPTED' and len(final['result']['cases']) == 4
        assert len(candidate['snapshot']['edits']) == 1
        artifact = {'task_id': identity, 'task_sha256': value['sha256'], 'initial_files': value['files'],
                    'candidate': candidate, 'final_grade': final, 'origin': 'scripted_known_solution'}
        (output / (identity + '.json')).write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')
        results.append({'task_id': identity, 'starter': 'WRONG_ANSWER', 'public_after': 'ACCEPTED',
                        'final': final['result']['verdict'], 'final_cases': len(final['result']['cases']),
                        'assistant_edits': len(candidate['snapshot']['edits']), 'scripted_responses': candidate['model_responses']})
    assert not (await DockerJava(profile).must(['ps', '-aq', '--filter', 'label=interview-agent.executor=ja06'])).strip()
    receipt = {'status': 'passed', 'scope': 'JA-07-01 local controlled scripted core; not live model/API/SQL/production',
               'isolation_checks': isolation['count'], 'tasks': results, 'new_model_http_calls': 0, 'new_model_cost_cny': 0,
               'remaining_candidate_containers': 0}
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', type=Path, default=ROOT / 'configs/ja06-docker-java.json')
    p.add_argument('--task', choices=[*TASKS, 'all'], default='all')
    p.add_argument('--output', type=Path, default=ROOT / '.private/ja-07' / ('core-' + uuid4().hex))
    args = p.parse_args()
    result = asyncio.run(check(DockerProfile.load(args.profile), list(TASKS) if args.task == 'all' else [args.task], args.output))
    print(json.dumps({**result, 'receipt': str(args.output / 'receipt.json')}, ensure_ascii=False))
