"""JA-03 browser chain. --live spends the explicit shared CNY2 authorization."""
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
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import DatabaseSettings,Settings
from interview_agent.storage.migrate import migrate
from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import authorize
from interview_agent.career.billing import CareerPolicy,CareerLedger
from interview_agent.career.delegation_billing import AUTHORIZATION_ID

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('old_web_runner',ROOT/'scripts/check-python-web.py')
web=importlib.util.module_from_spec(spec);spec.loader.exec_module(web)


async def prepare(live):
    owner=DatabaseSettings.model_validate_json((ROOT/('.private/postgres/migration.json' if live else '.private/postgres/test-migration.json')).read_text(encoding='utf-8'))
    assert owner.dbname==('py_interview' if live else 'py_interview_test')
    await migrate(owner,ROOT/'backend/migrations')
    if live:
        load_key(ROOT/'.private/ja-03/credentials.env')
        await authorize(owner,CareerPolicy.load(ROOT/'configs/ja03-deepseek-flash.json'),AUTHORIZATION_ID,2_000_000_000)
    return owner


async def audit():
    settings=Settings.load(ROOT/'.private/postgres/app.json');pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    try:
        await pool.open(wait=True)
        return await CareerLedger(pool,CareerPolicy.load(ROOT/'configs/ja03-deepseek-flash.json'),AUTHORIZATION_ID).summary()
    finally:await pool.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    for port in (3100,5174):
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    owner=asyncio.run(prepare(args.live),loop_factory=selector_loop)
    run_id=str(uuid4());output=ROOT/'.private/ja-03'/('live' if args.live else 'scripted')/run_id;output.mkdir(parents=True)
    if args.live:(output/'billing-before.json').write_text(json.dumps(asyncio.run(audit(),loop_factory=selector_loop)),encoding='utf-8')
    processes=[];handles=[];code=1
    try:
        config='.private/postgres/app.json' if args.live else '.private/postgres/test.json'
        for name,command in [('api',[sys.executable,'-m','interview_agent.api','--config',config]),('web',['node','apps/web/tests/serve-built.mjs'])]:
            handle=(output/f'{name}.log').open('wb');handles.append(handle)
            processes.append(subprocess.Popen(command,cwd=ROOT,stdout=handle,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW))
        web.wait_ready('http://127.0.0.1:3100/health',processes[0]);web.wait_ready('http://127.0.0.1:5174/',processes[1])
        env={**os.environ,'JA03_RUN_ID':run_id,'JA03_OUTPUT':str(output),'JA03_PYTHON':sys.executable,'JA03_LIVE':'1' if args.live else '0','LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false'}
        with (output/'browser.log').open('wb') as log:
            code=subprocess.run(['node','apps/web/tests/delegation-flow.mts'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=480).returncode
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for handle in handles:handle.close()
        if args.live:(output/'billing-final.json').write_text(json.dumps(asyncio.run(audit(),loop_factory=selector_loop)),encoding='utf-8')
        cleaned=asyncio.run(web.cleanup(owner,run_id),loop_factory=selector_loop)
        (output/'runner.json').write_text(json.dumps({'exit_code':code,'cleaned_synthetic_users':cleaned,'owned_processes_stopped':all(p.poll() is not None for p in processes),'live':args.live}),encoding='utf-8')
    print(json.dumps({'status':'passed' if code==0 else 'failed','output':str(output)}));return code


if __name__=='__main__':raise SystemExit(main())
