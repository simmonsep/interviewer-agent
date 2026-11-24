"""One user-interactive, no-resume trial. Each invocation handles one model task."""
import argparse,asyncio,hashlib,json,secrets
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.app import create_app
from interview_agent.api.__main__ import selector_loop
from interview_agent.storage.config import Settings,DatabaseSettings
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport
from interview_agent.contracts.operations import QueueMessage
from interview_agent.materials.cli import ResumeClient,parser as cli_parser
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for
from interview_agent.interviews.billing import InterviewPolicy,InterviewLedger
from interview_agent.billing.ledger import authorize

ROOT=Path(__file__).resolve().parents[1]
PHASE='core-06-21-20261105-cny2'
POLICY=ROOT/'configs/core0621-interviews.json'
BANK=ROOT/'fixtures/question-banks/agent-developer-trial.json'


def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
def freeze():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in [*sorted((ROOT/'backend/src').rglob('*.py')),Path(__file__).resolve(),POLICY,BANK,ROOT/'uv.lock']}


async def run(a):
    live=a.mode=='live'
    if live and (not a.execute_live or a.max_cny!='2' or not a.credentials):raise ValueError('explicit_new_cny2_trial_approval_required')
    if not live and a.credentials:raise ValueError('scripted_must_not_read_real_credentials')
    api=Settings.load(a.api_config);settings=WorkerSettings.load(a.worker_config);owner=DatabaseSettings.model_validate_json(a.owner_config.read_text())
    expected='development' if live else 'test'
    if api.mode!=expected or settings.mode!=expected or owner.user!='py_interview_owner':raise ValueError('mode_owner_required')
    if any(getattr(api.database,k)!=getattr(settings.database,k) or getattr(api.database,k)!=getattr(owner,k) for k in ('host','port','dbname')) or owner.dbname!=('py_interview' if live else 'py_interview_test'):raise ValueError('database_pair_required')
    out=a.workspace.resolve()
    if '.private' not in out.parts:raise ValueError('private_workspace_required')
    policy=InterviewPolicy.load(POLICY)
    if a.answer_file:
        plan=json.loads((out/'plan.json').read_text())
        if plan['mode']!=a.mode or plan['frozen']!=freeze():raise ValueError('trial_context_changed')
    else:
        out.mkdir(parents=True,exist_ok=False);plan={'phase':PHASE if live else 'scripted-'+PHASE+'-'+uuid4().hex,'mode':a.mode,'max_cny':2,'http_cap':12,'tasks':3,'user_answers_only':live,'frozen':freeze()};write(out/'plan.json',plan)
        if live:
            async with await AsyncConnection.connect(owner.conninfo()) as c:
                if await (await c.execute('SELECT id FROM app.model_authorizations WHERE id=%s',(PHASE,))).fetchone():raise ValueError('trial_already_initialized_no_retry')
            await authorize(owner,policy,PHASE,2_000_000_000)
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row});redis=settings.redis.connect();stream='py-interview:'+expected+':'+uuid4().hex+':jobs:v1:interviews'
    try:
        await pool.open(wait=True)
        with TestClient(create_app(api),base_url=api.origin,backend_options={'loop_factory':selector_loop},raise_server_exceptions=False) as client:
            cli=ResumeClient(client,out/'session.json')
            def command(*args):return cli.execute(cli_parser().parse_args(['--state',str(out/'session.json'),*map(str,args)]))
            if not a.answer_file:
                user=cli.execute(cli_parser().parse_args(['register','--email',plan['phase']+'@example.com','--name','User trial']),password=secrets.token_urlsafe(24))
                write(out/'trial-user.json',{'user_id':user['user']['id']})
                g=command('goal-create','--direction','Python Agent开发（LangChain、LangGraph、工具调用、状态与记忆）','--intent','training')
                preview=cli.request('/api/question-banks/imports',{'document':json.loads(BANK.read_text(encoding='utf-8'))});write(out/'bank-preview.json',preview)
                bank=cli.request('/api/question-banks/imports/'+preview['preview_id']+'/commit',{})
                s=cli.request(cli.base()+'/interviews',{'expected_version':1,'role':'ai-application','mode':'interview','max_questions':2,'duration_seconds':3600,'question_banks':[bank],'allow_project_questions':False})
                cli.saved.setdefault('interviews',{})[g['goal_id']]=s['id'];cli.save()
            else:
                s=command('interview-show')
                if s['status']!='WAITING_FOR_USER':raise ValueError('trial_not_waiting_for_user_no_retry')
                s=command('interview-submit',a.answer_file)
            async with pool.connection() as c:
                row=await (await c.execute("SELECT * FROM app.career_interview_runs WHERE session_id=%s AND status='PENDING' ORDER BY created_at,id LIMIT 1",(s['id'],))).fetchone()
            if not row:raise ValueError('pending_owned_task_required')
            q=QueueTransport(redis,stream);await q.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
            try:
                await worker_for(InterviewRepository(pool),q,provider='live' if live else 'scripted',policy=policy,authorization=PHASE,credentials=a.credentials).once()
            finally:
                s=command('interview-show');fees=await InterviewLedger(pool,policy,PHASE).summary() if live else {'usage_status':'not_called','accounted_upper_nano':0,'http_attempts':0}
                n=len([e for e in s['events'] if e['kind']=='answer']);write(out/f'turn-{n}-session.json',s);write(out/'fees.json',fees)
            if fees['usage_status']=='unknown' or s['status'] not in ('WAITING_FOR_USER','COMPLETED'):raise ValueError('trial_paused_no_automatic_retry')
            if s['status']=='WAITING_FOR_USER':
                q=next(e for e in reversed(s['events']) if e['kind']=='question');answer=out/('answer-'+q['id']+'.json')
                command('interview-answer-file','--output',answer)
                print(json.dumps({'status':s['status'],'question':q['payload']['text'],'answer_file':str(answer),'fees_upper_nano':fees['accounted_upper_nano']},ensure_ascii=False))
            else:print(json.dumps({'status':'COMPLETED','report':s['report'],'fees_upper_nano':fees['accounted_upper_nano']},ensure_ascii=False))
    finally:
        await redis.delete(stream);await redis.aclose();await pool.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['scripted','live'],default='scripted')
    for n in ['api-config','worker-config','owner-config','workspace']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--credentials',type=Path)
    p.add_argument('--execute-live',action='store_true');p.add_argument('--max-cny');p.add_argument('--answer-file',type=Path)
    asyncio.run(run(p.parse_args()),loop_factory=selector_loop)
