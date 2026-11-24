"""JA-02 browser acceptance; optional anonymous real sources, always zero LLM calls."""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from uuid import uuid4
from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import DatabaseSettings
from interview_agent.storage.migrate import migrate

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('old_web_runner',ROOT/'scripts/check-python-web.py')
web=importlib.util.module_from_spec(spec);spec.loader.exec_module(web)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-live',action='store_true');args=parser.parse_args()
    for port in (3100,5174):
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    owner=DatabaseSettings.model_validate_json((ROOT/'.private/postgres/test-migration.json').read_text(encoding='utf-8'))
    assert owner.dbname=='py_interview_test'
    asyncio.run(migrate(owner,ROOT/'backend/migrations'),loop_factory=selector_loop)
    run_id=str(uuid4());output=ROOT/'.private/ja-02'/('source-live' if args.source_live else 'scripted')/run_id;output.mkdir(parents=True)
    processes=[];handles=[];code=1
    try:
        for name,command in [('api',[sys.executable,'-m','interview_agent.api','--config','.private/postgres/test.json']),('web',['node','apps/web/tests/serve-built.mjs'])]:
            handle=(output/f'{name}.log').open('wb');handles.append(handle)
            processes.append(subprocess.Popen(command,cwd=ROOT,stdout=handle,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW))
        web.wait_ready('http://127.0.0.1:3100/health',processes[0]);web.wait_ready('http://127.0.0.1:5174/',processes[1])
        env={**os.environ,'JA02_RUN_ID':run_id,'JA02_OUTPUT':str(output),'JA02_PYTHON':sys.executable,'JA02_SOURCE_LIVE':'1' if args.source_live else '0'}
        with (output/'browser.log').open('wb') as log:
            code=subprocess.run(['node','apps/web/tests/research-flow.mts'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=360).returncode
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for handle in handles:handle.close()
        cleaned=asyncio.run(web.cleanup(owner,run_id),loop_factory=selector_loop)
        (output/'runner.json').write_text(json.dumps({'exit_code':code,'cleaned_synthetic_users':cleaned,'owned_processes_stopped':all(p.poll() is not None for p in processes),'source_live':args.source_live,'model_calls':0}),encoding='utf-8')
    print(json.dumps({'status':'passed' if code==0 else 'failed','output':str(output)}));return code


if __name__=='__main__':raise SystemExit(main())
