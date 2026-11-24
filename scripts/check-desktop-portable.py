"""D-00-03 real native preview outside the source tree; synthetic/test only."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    source = args.bundle.resolve()
    assert source.is_relative_to((ROOT / '.private/d-00/portable').resolve())
    external = Path('E:/codex/toolchains/desktop-previews') / uuid4().hex
    app = external / 'app'
    shutil.copytree(source, app)
    # Configuration is separately provisioned test data, never assembled into the app.
    settings = external / 'settings'
    settings.mkdir()
    shutil.copyfile(ROOT / '.private/postgres/test.json', settings / 'api.json')
    shutil.copyfile(ROOT / '.private/redis/test-worker.json', settings / 'worker.json')
    assert not (app / 'settings').exists()
    assert not any(p.suffix == '.env' or p.name in {'api.json', 'worker.json', 'credentials.env'} for p in app.rglob('*'))
    for relative in ('api.json', 'worker.json'):
        assert b'DEEPSEEK_API_KEY' not in (settings / relative).read_bytes()
    env = {k:v for k,v in os.environ.items() if k not in {
        'INTERVIEW_PROBE_ROOT','INTERVIEW_DESKTOP_RESOURCES','INTERVIEW_DESKTOP_CONFIG_DIR','INTERVIEW_PROBE_OUTPUT'}}
    env.update(INTERVIEW_PROBE_AUTORUN='1',INTERVIEW_DESKTOP_CONFIG_DIR=str(settings))
    cases = []

    def run(name, expected, *, config=settings, resources=None, resume=None):
        out = resume or external / name
        out.mkdir(exist_ok=True)
        run_env = dict(env, INTERVIEW_PROBE_OUTPUT=str(out), INTERVIEW_DESKTOP_CONFIG_DIR=str(config))
        if resources is not None:
            run_env['INTERVIEW_DESKTOP_RESOURCES'] = str(resources)
        with (out / f'{name}-host.log').open('wb') as log:
            child = subprocess.Popen([str(app/'interview-desktop-probe.exe')], cwd=external, env=run_env, stdout=log, stderr=log)
            try:
                assert child.wait(timeout=120) == 0, 'native_host_failed'
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
        assert (out / 'frontend-finished.txt').exists(), 'react_native_completion_missing'
        status = json.loads((out / 'startup.json').read_text(encoding='utf-8'))
        assert status['code'] == expected, status
        if expected == 'ready':
            receipt = json.loads((out / 'receipt.json').read_text(encoding='utf-8'))
            assert receipt['status'] == 'passed'
            (out / f'{name}-receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        else:
            assert not (out / 'receipt.json').exists(), 'blocked_start_must_not_run_task'
            assert not (out / 'sidecar-pid.txt').exists(), 'blocked_start_must_not_spawn_api'
        cases.append({'name':name, 'code':status['code'], 'passed':True})
        return out

    run('missing-resources','python_resource_missing',resources=external/'missing-python')
    run('missing-config','configuration_missing',config=external/'missing-config')
    invalid = external/'invalid'
    invalid.mkdir()
    (invalid/'api.json').write_text('{invalid json',encoding='utf-8')
    shutil.copyfile(settings/'worker.json', invalid/'worker.json')
    run('invalid-config','configuration_invalid',config=invalid)
    # Simulate unreachable dependencies through separate config, never stop shared services.
    with socket.socket() as unused:
        unused.bind(('127.0.0.1',0))
        closed_port = unused.getsockname()[1]
    for dependency, expected in [('postgres','postgres_unavailable'),('redis','redis_unavailable')]:
        config = external / dependency
        config.mkdir()
        api = json.loads((settings/'api.json').read_text(encoding='utf-8'))
        worker = json.loads((settings/'worker.json').read_text(encoding='utf-8'))
        if dependency == 'postgres':
            api['database']['port'] = worker['database']['port'] = closed_port
        else:
            worker['redis']['port'] = closed_port
        (config/'api.json').write_text(json.dumps(api),encoding='utf-8')
        (config/'worker.json').write_text(json.dumps(worker),encoding='utf-8')
        run(dependency,expected,config=config)
    with socket.socket() as sentinel:
        sentinel.bind(('127.0.0.1',3100))
        sentinel.listen()
        run('occupied-port','port_3100_in_use')
        assert sentinel.getsockname()[1] == 3100
    out = run('success','ready')
    run('resume','ready',resume=out)
    first = json.loads((out/'success-receipt.json').read_text())
    second = json.loads((out/'resume-receipt.json').read_text())
    assert first['goal_id'] == second['goal_id'] and second['restored_after_host_restart']
    with socket.socket() as port:
        port.bind(('127.0.0.1',3100))
    report={'status':'passed','scope':'private_windows_portable_preview_not_installer','outside_source_tree':True,
            'app_has_no_private_configuration':True,'cases':cases,'model_calls':0,'summary_path':str(external/'summary.json')}
    (external/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
