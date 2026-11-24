"""Windows: export Git source, bootstrap locked Python, and verify fresh PG/Redis core.

Usage: python scripts/reproduce-core.py [--revision HEAD] [--proxy http://127.0.0.1:7897]
All new files live under --output (default .private/ja-09-03/rebuild/UUID).
Own containers are stopped; volumes, logs and generated private credentials are retained.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from uuid import uuid4
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def write(path,value):
    path.write_bytes((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())


def free_port(exclude=()):
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        if port not in exclude:return port


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--revision',default='HEAD');p.add_argument('--output',type=Path);p.add_argument('--proxy')
    args=p.parse_args(argv)
    if sys.platform!='win32':p.error('This bootstrap is validated for Windows only')
    output=(args.output or ROOT/'.private/ja-09-03/rebuild'/uuid4().hex).resolve()
    output.mkdir(parents=True,exist_ok=False)
    source=output/'source';logs=output/'logs';logs.mkdir();temp=output/'temp';temp.mkdir()
    env=dict(os.environ);env.update(TEMP=str(temp),TMP=str(temp),PYTHONUTF8='1',
        UV_PROJECT_ENVIRONMENT=str(source/'.venv'),UV_PYTHON_PREFERENCE='only-managed',GIT_CEILING_DIRECTORIES=str(output))
    for key in ['DEEPSEEK_API_KEY','OPENAI_API_KEY','UV_PYTHON','GIT_DIR','GIT_WORK_TREE','PY_INTERVIEW_TEST_CONFIG','PY_INTERVIEW_WORKER_TEST_CONFIG','PY_INTERVIEW_EXECUTOR_PROFILE']:
        env.pop(key,None)
    receipt={'status':'failed','scope':'Windows Python backend core, independent source/Python/dependencies/PG/Redis; shared host/Docker engine/images',
        'model_provider_calls':0,'new_model_cost_cny':0,'steps':[],'resources':{},'containers_stopped':False}
    configs=[];current='archive'

    def run(stage,command,cwd=source,child_env=None):
        nonlocal current
        current=stage
        with (logs/(stage+'.log')).open('wb') as log:
            result=subprocess.run([str(x) for x in command],cwd=cwd,env=child_env or env,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
        receipt['steps'].append({'stage':stage,'exit_code':result.returncode})
        write(output/'receipt.json',receipt)
        if result.returncode:raise RuntimeError(stage+'_failed')

    try:
        tree=subprocess.check_output(['git','rev-parse','--verify','--end-of-options',args.revision+'^{tree}'],cwd=ROOT,text=True).strip()
        archive=output/'source.zip';run('archive',['git','archive','--format=zip','--output',archive,tree],ROOT)
        receipt.update(source_tree=tree,source_reference=args.revision,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        with zipfile.ZipFile(archive) as z:
            if any(n.startswith(('.private/','.venv/','.bootstrap/','.git/')) for n in z.namelist()):raise RuntimeError('private_or_runtime_files_in_archive')
            z.extractall(source)
        receipt['no_private_or_runtime_files_before_bootstrap']=all(not (source/n).exists() for n in ['.private','.venv','.bootstrap','.git'])
        prefix='py-repro-'+uuid4().hex[:12];pg=free_port();redis=free_port([pg])
        for name,port in [('postgres',pg),('redis',redis)]:
            path=source/('configs/python-'+name+'.json');c=json.loads(path.read_text(encoding='utf-8'))
            c.update(container=prefix+'-'+name,volume=prefix+'-'+name+'-data',port=port)
            write(path,c);configs.append(c)
        receipt['resources']={name:{k:c[k] for k in ['container','volume','host','port','image']} for name,c in zip(['postgres','redis'],configs)}
        bootstrap_env=dict(env)
        if args.proxy:bootstrap_env.update(HTTP_PROXY=args.proxy,HTTPS_PROXY=args.proxy)
        shell=shutil.which('pwsh') or shutil.which('powershell')
        if not shell:raise RuntimeError('powershell_required')
        run('bootstrap',[shell,'-NoProfile','-ExecutionPolicy','Bypass','-File',source/'scripts/bootstrap-python.ps1'],child_env=bootstrap_env)
        python=source/'.venv/Scripts/python.exe'
        doctor="""import sys,json,importlib.metadata as m
from pathlib import Path
root=Path.cwd().resolve()
assert Path(sys.prefix).resolve()==root/'.venv'
assert Path(sys.base_prefix).resolve().is_relative_to(root/'.bootstrap/python')
import interview_agent
assert Path(interview_agent.__file__).resolve().is_relative_to(root/'backend/src')
packages=[{'name':d.metadata['Name'],'version':d.version,'license_expression':d.metadata.get('License-Expression'),'license_metadata':d.metadata.get('License')} for d in m.distributions()]
Path('.private').mkdir(exist_ok=True)
Path('.private/rebuild-environment.json').write_bytes(json.dumps({'python':sys.version,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'package_source':interview_agent.__file__,'packages':packages},ensure_ascii=False,indent=2).encode())
print(json.dumps({'independent_python_and_source':True,'installed_distributions':len(packages)}))
"""
        run('environment',[python,'-X','utf8','-c',doctor])
        run('postgres',[python,'-m','interview_agent.storage.provision','--root',source])
        run('redis',[python,'-m','interview_agent.worker.provision','--root',source])
        empty="""import json,psycopg,redis
from pathlib import Path
from interview_agent.storage.config import DatabaseSettings
from interview_agent.worker.config import WorkerSettings
for name in ['migration.json','test-migration.json']:
 c=DatabaseSettings.model_validate_json((Path('.private/postgres')/name).read_text())
 with psycopg.connect(c.conninfo()) as db:
  assert db.execute(\"SELECT count(*) FROM pg_tables WHERE schemaname IN ('app','auth','infra')\").fetchone()[0]==0
for name in ['worker.json','test-worker.json']:
 c=WorkerSettings.load(Path('.private/redis')/name).redis
 with redis.Redis(host=c.host,port=c.port,db=c.db,password=c.password.get_secret_value()) as r:assert r.dbsize()==0
print(json.dumps({'both_pg_schemas_empty':True,'both_redis_databases_empty':True}))
"""
        run('empty-services',[python,'-c',empty])
        for name in ['migration.json','test-migration.json']:
            run('migrate-'+name.removesuffix('.json'),[python,'-m','interview_agent.storage','--config',source/'.private/postgres'/name])
        test_env=dict(env);test_env.update(PY_INTERVIEW_TEST_CONFIG=str(source/'.private/postgres/test.json'),
            PY_INTERVIEW_WORKER_TEST_CONFIG=str(source/'.private/redis/test-worker.json'),
            PY_INTERVIEW_EXECUTOR_PROFILE=str(source/'configs/ja06-docker-java.json'))
        run('pytest',[python,'-m','pytest','-q','--basetemp',temp/'pytest','--junitxml',output/'pytest.xml'],child_env=test_env)
        # Real network HTTP examples, one at a time because they share the declared 3100 API origin.
        for script in ['check-career-application-loop.py','check-written-api.py','check-coding-api.py']:
            run(script.removesuffix('.py'),[python,source/'scripts'/script])
        final="""import json,hashlib,psycopg
from pathlib import Path
from interview_agent.storage.config import DatabaseSettings
summary={}
for n in ['migration.json','test-migration.json']:
 c=DatabaseSettings.model_validate_json((Path('.private/postgres')/n).read_text())
 with psycopg.connect(c.conninfo()) as db:
  rows=db.execute('SELECT name,sha256 FROM infra.schema_migrations ORDER BY name').fetchall()
  assert len(rows)==len(list(Path('backend/migrations').glob('[0-9][0-9][0-9]_*.sql'))) and all(hashlib.sha256((Path('backend/migrations')/name).read_bytes()).hexdigest()==h for name,h in rows)
  calls=db.execute('SELECT count(*) FROM app.model_http_calls').fetchone()[0];assert calls==0
  summary[c.dbname]={'migrations':len(rows),'hashes_match':True,'model_http_rows':calls}
print(json.dumps(summary))
"""
        run('final-databases',[python,'-c',final])
        receipt['status']='passed'
    except Exception as exc:
        receipt.update(failed_stage=current,error_type=type(exc).__name__)
    finally:
        stopped=True;cleanup=[]
        for c in configs:
            try:
                check=subprocess.run(['docker','inspect','--format','{{index .Config.Labels "io.interview-agent.project"}}',c['container']],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
                if check.returncode==0 and check.stdout.strip()==str(source.resolve()):
                    stop=subprocess.run(['docker','stop',c['container']],capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
                    ok=stop.returncode==0;stopped=stopped and ok
                    cleanup.append({'container':c['container'],'status':'stopped' if ok else 'stop_failed'})
                elif check.returncode and 'No such object' in check.stderr:
                    cleanup.append({'container':c['container'],'status':'not_created'})
                else:
                    stopped=False;cleanup.append({'container':c['container'],'status':'inspection_or_ownership_failed'})
            except (OSError,subprocess.TimeoutExpired) as exc:
                stopped=False;cleanup.append({'container':c['container'],'status':type(exc).__name__})
        receipt['cleanup']=cleanup
        receipt['containers_stopped']=stopped
        receipt['volumes_and_private_evidence_retained']=True
        if not stopped:receipt['status']='failed'
        write(output/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'failed_stage':receipt.get('failed_stage'),'output':str(output),'model_provider_calls':0},ensure_ascii=False))
    return 0 if receipt['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
