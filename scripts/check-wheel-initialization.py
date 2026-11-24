"""Verify an installed core wheel against new, labelled PG/Redis resources; retain volumes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
FLAGS=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0


def write(p,v):p.write_bytes((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode())


def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main(args):
    out=(args.output or ROOT/'.private/core0602/fresh'/uuid4().hex).resolve();out.mkdir(parents=True,exist_ok=False)
    (out/'configs').mkdir();outside=out/'outside';outside.mkdir();(out/'logs').mkdir()
    env=dict(os.environ);env.update(PYTHONUTF8='1',TEMP=str(out/'temp'),TMP=str(out/'temp'));(out/'temp').mkdir()
    for k in ['PYTHONPATH','PYTHONHOME','DEEPSEEK_API_KEY','OPENAI_API_KEY']:env.pop(k,None)
    configs=[];receipt={'status':'failed','provider_http':0,'model_cost_cny':0,'steps':[],
        'scope':'Installed wheel, new venv, shared Python base/host/Docker images; not full release or process-crash validation'}
    stage='installed-resources'
    def run(name,command):
        nonlocal stage
        stage=name
        with (out/'logs'/f'{name}.log').open('wb') as log:
            r=subprocess.run([str(x) for x in command],cwd=outside,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=FLAGS)
        receipt['steps'].append({'name':name,'exit_code':r.returncode})
        if r.returncode:raise RuntimeError(name+'_failed')
    py=args.python.resolve()
    try:
        doctor="""import sys,json
from pathlib import Path
import interview_agent
from interview_agent.resources import resource_root
from interview_agent.question_banks.defaults import defaults
from interview_agent.data.catalog import load_catalog
r=resource_root();assert r.is_relative_to(Path(sys.prefix))
assert Path(interview_agent.__file__).is_relative_to(Path(sys.prefix))
assert len(defaults())==3 and load_catalog(r)
print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'module':interview_agent.__file__,'resources':str(r)}))
"""
        run('installed-resources',[py,'-I','-c',doctor])
        prefix='py-core0602-'+uuid4().hex[:10];ports=set()
        for kind in ['postgres','redis']:
            cfg=json.loads((ROOT/f'configs/python-{kind}.json').read_text(encoding='utf-8'))
            port=free_port()
            while port in ports:port=free_port()
            ports.add(port);cfg.update(container=prefix+'-'+kind,volume=prefix+'-'+kind+'-data',port=port)
            write(out/f'configs/python-{kind}.json',cfg);configs.append(cfg)
        receipt['resources']=[{k:c[k] for k in ('container','volume','port','image')} for c in configs]
        run('postgres',[py,'-I','-m','interview_agent.storage.provision','--root',out])
        run('redis',[py,'-I','-m','interview_agent.worker.provision','--root',out])
        empty="""import sys,json,psycopg
from pathlib import Path
from interview_agent.storage.config import DatabaseSettings
from interview_agent.worker.config import WorkerSettings
root=Path(sys.argv[1])
for name in ['migration','test-migration']:
 cfg=DatabaseSettings.model_validate_json((root/f'.private/postgres/{name}.json').read_text())
 with psycopg.connect(cfg.conninfo()) as c:assert c.execute("SELECT count(*) FROM pg_tables WHERE schemaname IN ('app','auth','infra')").fetchone()[0]==0
for name in ['worker','test-worker']:
 cfg=WorkerSettings.load(root/f'.private/redis/{name}.json').redis
 import redis
 with redis.Redis(host=cfg.host,port=cfg.port,db=cfg.db,password=cfg.password.get_secret_value()) as c:assert c.dbsize()==0
print(json.dumps({'two_pg_empty':True,'two_redis_empty':True}))
"""
        run('empty-services',[py,'-I','-c',empty,out])
        for name in ['migration','test-migration']:
            for pass_id in ['first','repeat']:
                run(name+'-'+pass_id,[py,'-I','-m','interview_agent.storage','--config',out/f'.private/postgres/{name}.json'])
        smoke="""import json,sys,hashlib,psycopg,secrets,subprocess,socket,time
from pathlib import Path
import httpx
from interview_agent.storage.config import DatabaseSettings
from interview_agent.resources import resource_root
root=Path(sys.argv[1]);uid=None;owner=DatabaseSettings.model_validate_json((root/'.private/postgres/test-migration.json').read_text())
with socket.socket() as s:s.bind(('127.0.0.1',3100))
with (root/'logs/api.log').open('wb') as log:
 p=subprocess.Popen([sys.executable,'-I','-m','interview_agent.api','--config',str(root/'.private/postgres/test.json')],stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
 try:
  with httpx.Client(base_url='http://127.0.0.1:3100',headers={'Origin':'http://127.0.0.1:3100'},trust_env=False) as c:
   end=time.monotonic()+20
   while True:
    assert p.poll() is None
    try:
     if c.get('/health').status_code==200:break
    except httpx.HTTPError:pass
    if time.monotonic()>end:raise RuntimeError('api_timeout')
    time.sleep(.2)
   response=c.post('/api/auth/sign-up/email',json={'email':'wheel-'+secrets.token_hex(8)+'@example.com','password':secrets.token_urlsafe(24),'name':'Synthetic'})
   assert response.status_code==200;uid=response.json()['user']['id']
   banks=c.get('/api/question-banks');assert banks.status_code==200 and len(banks.json()['banks'])==3
   g=c.post('/api/career-goals/',json={'message':'检验wheel默认题库'},headers={'Idempotency-Key':secrets.token_hex(16)});assert g.status_code==202
   w=c.post('/api/career-goals/'+g.json()['id']+'/written',json={'expected_version':1,'role':'java-backend','question_banks':[{'bank_id':'00000000-0000-4000-8000-000000000003','version':1}]},headers={'Idempotency-Key':secrets.token_hex(16)})
   assert w.status_code==202 and len(w.json()['questions'])==3
   report={'real_network_http':True,'default_banks':3,'written_questions':3,'provider_http':0}
 finally:
  p.terminate()
  try:p.wait(timeout=10)
  except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
  if uid:
   with psycopg.connect(owner.conninfo(),autocommit=True) as c:c.execute('DELETE FROM auth.users WHERE id=%s',(uid,))
result={}
expected={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (resource_root()/'backend/migrations').glob('*.sql')}
for name in ['migration','test-migration']:
 cfg=DatabaseSettings.model_validate_json((root/f'.private/postgres/{name}.json').read_text())
 with psycopg.connect(cfg.conninfo()) as c:
  rows=c.execute('SELECT name,sha256 FROM infra.schema_migrations ORDER BY name').fetchall();assert dict(rows)==expected
  assert c.execute('SELECT count(*) FROM app.model_http_calls').fetchone()[0]==0
 result[name]={'migrations':len(rows),'hashes_match_installed_wheel':True,'model_calls':0}
(root/'installed-smoke.json').write_bytes(json.dumps({'api':report,'databases':result},indent=2).encode())
print(json.dumps(result))
"""
        run('installed-api-and-hashes',[py,'-I','-c',smoke,out]);receipt['status']='passed'
    except Exception as error:receipt.update(failed_stage=stage,error_type=type(error).__name__)
    finally:
        stopped=[]
        for cfg in configs:
            info=subprocess.run(['docker','inspect','--format','{{index .Config.Labels "io.interview-agent.project"}}',cfg['container']],capture_output=True,text=True,creationflags=FLAGS)
            if info.returncode==0 and info.stdout.strip()==str(out):
                result=subprocess.run(['docker','stop',cfg['container']],capture_output=True,creationflags=FLAGS);stopped.append(result.returncode==0)
            else:stopped.append(info.returncode!=0 and 'No such object' in info.stderr)
        receipt['owned_containers_stopped']=all(stopped);receipt['volumes_and_private_evidence_retained']=True
        if not all(stopped):receipt['status']='failed'
        write(out/'receipt.json',receipt)
    print(json.dumps({'status':receipt['status'],'output':str(out),'failed_stage':receipt.get('failed_stage')}))
    return 0 if receipt['status']=='passed' else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--python',type=Path,required=True);p.add_argument('--output',type=Path)
    raise SystemExit(main(p.parse_args()))
