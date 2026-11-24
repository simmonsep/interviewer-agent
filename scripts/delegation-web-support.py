"""One this-run synthetic goal phase, using a private stream and shared JA-03 budget."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID,uuid4
import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport,TaskWorker
from interview_agent.contracts.operations import QueueMessage
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for as coordinator
from interview_agent.career.research_repository import ResearchRepository
from interview_agent.career.research_worker import worker_for as researcher
from interview_agent.career.billing import CareerPolicy
from interview_agent.career.delegation_billing import AUTHORIZATION_ID
from interview_agent.career.research_agent import research
from interview_agent.runtime.types import AssistantResponse,ToolCall

ROOT=Path(__file__).resolve().parents[1]


async def run(args):
    settings=WorkerSettings.load(ROOT/('.private/redis/worker.json' if args.live else '.private/redis/test-worker.json'))
    assert (settings.mode,settings.database.dbname,settings.redis.db)==(('development','py_interview',0) if args.live else ('test','py_interview_test',1))
    if args.live and args.source_scenario!='ok':raise ValueError('live_source_failure_injection_refused')
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row});redis=settings.redis.connect()
    suffix='career' if args.phase=='coordinator' else 'research';stream=f'py-interview:{settings.mode}:{uuid4().hex}:jobs:v1:{suffix}'
    fixture=json.loads((ROOT/'fixtures/career-research/http.json').read_text(encoding='utf-8'))
    def respond(request):
        if args.source_scenario=='rate_limited':return httpx.Response(429)
        return httpx.Response(200,json=fixture['list'] if request.method=='POST' else fixture['details'][request.url.params['id']])
    try:
        await pool.open(wait=True)
        table='career_steps' if args.phase=='coordinator' else 'career_research_runs'
        async with pool.connection() as conn:
            owner=await (await conn.execute('SELECT u.email FROM app.career_goals g JOIN auth.users u ON u.id=g.user_id WHERE g.id=%s',(args.goal,))).fetchone()
            assert owner and owner['email'].startswith(f'pyweb-{args.run_id}-'),'only_this_run_synthetic_goal'
            row=await (await conn.execute(f"SELECT id,job_id,goal_version FROM app.{table} WHERE goal_id=%s AND status IN ('PENDING','RUNNING') ORDER BY created_at DESC LIMIT 1",(args.goal,))).fetchone()
        assert row,'pending_phase_required'
        queue=QueueTransport(redis,stream)
        await queue.publish(QueueMessage(operation_id=str(row['id']),job_id=str(row['job_id']),resource_version=row['goal_version']))
        policy=CareerPolicy.load(ROOT/'configs/ja03-deepseek-flash.json')
        options={'policy':policy,'authorization':AUTHORIZATION_ID,'credentials':ROOT/'.private/ja-03/credentials.env'}
        if args.phase=='coordinator':worker=coordinator(CareerRepository(pool),queue,mode='live' if args.live else 'scripted',**options)
        elif args.source_scenario=='ask_before_query':
            repo=ResearchRepository(pool)
            async def prepare_question(claim,lost):
                class Clarifier:
                    count=0
                    async def complete(self,messages,specs):
                        await repo.admit_scripted(claim);self.count+=1
                        return AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name='read_research_scope' if self.count==1 else 'ask_user',arguments={} if self.count==1 else {'question':'请先确认研究方向。'})])
                def forbidden(request):raise AssertionError('no source request before clarification')
                async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                    bundle=await research(await repo.agent_scope(claim),Clarifier(),client,lost,origin='offline_fixture')
                await repo.save(claim,bundle,'scripted_protocol')
            worker=TaskWorker(repo,queue,'clarify-'+uuid4().hex,prepare=prepare_question)
        else:worker=researcher(ResearchRepository(pool),queue,provider='live' if args.live else 'scripted',
                    inner_factory=None if args.live else lambda:httpx.MockTransport(respond),origin='source_live' if args.live else 'offline_fixture',**options)
        assert await worker.once()
        async with pool.connection() as conn:
            result=await (await conn.execute(f'SELECT status,model_requests FROM app.{table} WHERE id=%s',(row['id'],))).fetchone()
            totals=await (await conn.execute('SELECT model_requests,source_requests,status AS goal_status FROM app.career_goals WHERE id=%s',(args.goal,))).fetchone()
        print(json.dumps({'phase':args.phase,**dict(result),'goal_status':totals['goal_status'],'goal_totals':dict(totals)},ensure_ascii=False))
    finally:
        await redis.delete(stream);await redis.aclose();await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('goal','run-id'):parser.add_argument('--'+name,required=True,type=lambda s:str(UUID(s)))
    parser.add_argument('--phase',choices=['coordinator','researcher'],required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--source-scenario',choices=['ok','rate_limited','ask_before_query'],default='ok')
    asyncio.run(run(parser.parse_args()),loop_factory=selector_loop)
