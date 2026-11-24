"""One scoped synthetic goal run for browser acceptance, never an LLM invocation."""
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
from interview_agent.worker.queue import QueueTransport
from interview_agent.contracts.operations import QueueMessage
from interview_agent.career.research_repository import ResearchRepository
from interview_agent.career.research_worker import worker_for

ROOT=Path(__file__).resolve().parents[1]


async def run(args):
    settings=WorkerSettings.load(ROOT/'.private/redis/test-worker.json')
    assert settings.mode=='test' and settings.database.dbname=='py_interview_test' and settings.redis.db==1
    if args.source_live and args.scenario!='ok':raise ValueError('live_cannot_inject_source_failure')
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect();stream=f'py-interview:test:{uuid4().hex}:jobs:v1:research'
    fixture=json.loads((ROOT/'fixtures/career-research/http.json').read_text(encoding='utf-8'))
    def respond(request):
        if args.scenario=='rate_limited':return httpx.Response(429)
        if request.method=='POST':return httpx.Response(200,json=fixture['list'])
        return httpx.Response(200,json=fixture['details'][request.url.params['id']])
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            row=await (await conn.execute('''SELECT g.id,u.email FROM app.career_goals g JOIN auth.users u ON u.id=g.user_id
                WHERE g.id=%s''',(args.goal,))).fetchone()
            # Two synthetic account shapes are accepted: the classic per-run pyweb
            # account and the desktop guest identity (local-*@desktop.example.com), which is
            # also a purely local synthetic account created by the product entry itself.
            email=row['email'] if row else ''
            assert row and (email.startswith(f'pyweb-{args.run_id}-') or email.startswith('local-') and email.endswith('@desktop.example.com')),'this_run_synthetic_goal_required'
            job=await (await conn.execute("SELECT id,job_id,goal_version FROM app.career_research_runs WHERE goal_id=%s AND status IN ('PENDING','RUNNING') ORDER BY created_at DESC LIMIT 1",(args.goal,))).fetchone()
        assert job,'pending_research_required'
        queue=QueueTransport(redis,stream);repo=ResearchRepository(pool)
        await queue.publish(QueueMessage(operation_id=str(job['id']),job_id=str(job['job_id']),resource_version=job['goal_version']))
        worker=worker_for(repo,queue) if args.source_live else worker_for(repo,queue,inner_factory=lambda:httpx.MockTransport(respond),origin='offline_fixture')
        assert await worker.once()
        async with pool.connection() as conn:
            result=await (await conn.execute('SELECT status,result,error_code FROM app.career_research_runs WHERE id=%s',(job['id'],))).fetchone()
        print(json.dumps(dict(result),ensure_ascii=False))
    finally:
        await redis.delete(stream);await redis.aclose();await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('goal','run-id'):parser.add_argument('--'+name,required=True,type=lambda x:str(UUID(x)))
    parser.add_argument('--source-live',action='store_true')
    parser.add_argument('--scenario',choices=['ok','rate_limited'],default='ok')
    asyncio.run(run(parser.parse_args()),loop_factory=selector_loop)
