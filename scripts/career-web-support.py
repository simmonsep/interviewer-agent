"""Scoped synthetic career worker for browser acceptance; --live explicitly opts in."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID,uuid4
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport,OutboxDispatcher
from interview_agent.career.repository import CareerRepository
from interview_agent.career.worker import worker_for
from interview_agent.career.billing import CareerLedger,CareerPolicy,AUTHORIZATION_ID

ROOT=Path(__file__).resolve().parents[1]


async def run(args):
    settings=WorkerSettings.load(ROOT/('.private/redis/worker.json' if args.live else '.private/redis/test-worker.json'))
    assert (settings.mode,settings.database.dbname,settings.redis.db)==(('development','py_interview',0) if args.live else ('test','py_interview_test',1))
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect(); stream=f'py-interview:{settings.mode}:{uuid4().hex}:jobs:v1:career'
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            row=await (await conn.execute('SELECT g.id,u.email FROM app.career_goals g JOIN auth.users u ON u.id=g.user_id WHERE g.id=%s',(args.goal,))).fetchone()
        # Desktop-shell acceptance signs in with the host-provided local identity instead of
        # a pyweb-* browser account; both are synthetic and never a real account.
        email=(row or {}).get('email') or ''
        assert row and (email.startswith(f'pyweb-{args.run_id}-')
                        or (email.startswith('local-') and email.endswith('@desktop.example.com'))), 'this_run_synthetic_goal_required'
        class ScopedRepository(CareerRepository):
            async def pending_dispatch(self,recovery_seconds=5):
                return [r for r in await super().pending_dispatch(recovery_seconds)
                    if str(r['operation_id']) in await ids()]
        async def ids():
            async with pool.connection() as conn:
                rows=await (await conn.execute('SELECT id FROM app.career_steps WHERE goal_id=%s',(args.goal,))).fetchall()
            return {str(r['id']) for r in rows}
        repo=ScopedRepository(pool); queue=QueueTransport(redis,stream)
        policy=CareerPolicy.load(ROOT/'configs/ja01-deepseek-flash.json')
        assert await OutboxDispatcher(repo,queue).dispatch()==1
        assert await worker_for(repo,queue,mode='live' if args.live else 'scripted',policy=policy,
            credentials=ROOT/'.private/ja-01/credentials.env').once()
        async with pool.connection() as conn:
            value=await (await conn.execute('SELECT status,version,model_requests,action FROM app.career_goals WHERE id=%s',(args.goal,))).fetchone()
        if args.live:
            audit=await CareerLedger(pool,policy,AUTHORIZATION_ID).summary()
            value={**value,'authorization_http_attempts':audit['http_attempts'],'accounted_upper_nano':audit['accounted_upper_nano']}
        print(json.dumps(dict(value),ensure_ascii=False))
    finally:
        await redis.delete(stream);await redis.aclose();await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goal',required=True,type=lambda x:str(UUID(x)))
    parser.add_argument('--run-id',required=True,type=lambda x:str(UUID(x)))
    parser.add_argument('--live',action='store_true')
    asyncio.run(run(parser.parse_args()),loop_factory=selector_loop)
