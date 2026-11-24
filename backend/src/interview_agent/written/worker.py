import argparse
import asyncio
from pathlib import Path
from uuid import uuid4
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.worker.config import WorkerSettings
from interview_agent.api.__main__ import selector_loop
from interview_agent.worker.queue import QueueTransport, OutboxDispatcher, TaskWorker
from interview_agent.execution.docker_java import DockerJava, DockerProfile, ExecutionUnavailable
from .repository import WrittenRepository


def worker_for(repo, queue, executor=None):
    async def prepare(claim,lost):
        row=await repo.context(claim)
        if row['candidate'] is not None: return
        if executor is None:
            await repo.save(claim,{'verdict':'ENVIRONMENT_ERROR','code':'written_executor_disabled','cases':[]}); return
        if row['input_payload']['policy']['image']!=executor.profile.image:
            await repo.save(claim,{'verdict':'ENVIRONMENT_ERROR','code':'executor_image_mismatch','cases':[]}); return
        if not await repo.begin(claim): return
        result=await executor.grade(row['source'],row['input_payload']['task']['cases'],lost)
        if not lost.is_set(): await repo.save(claim,result)
    return TaskWorker(repo,queue,'written-'+uuid4().hex,lease_seconds=6,prepare=prepare)


async def run(args):
    settings=WorkerSettings.load(args.config)
    executor=DockerJava(DockerProfile.load(args.profile)) if args.executor=='docker' else None
    if executor: await executor.readiness()
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row}); redis=settings.redis.connect()
    try:
        await pool.open(wait=True); repo=WrittenRepository(pool); queue=QueueTransport(redis,settings.stream+':written')
        worker=worker_for(repo,queue,executor)
        while True:
            await OutboxDispatcher(repo,queue).dispatch(); await worker.once()
            if args.once: return
            await asyncio.sleep(.3)
    finally: await redis.aclose(); await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Independent Java practice Worker; execution disabled by default.')
    parser.add_argument('--config',required=True,type=Path); parser.add_argument('--executor',choices=['disabled','docker'],default='disabled')
    parser.add_argument('--profile',type=Path,default=Path('configs/ja06-docker-java.json')); parser.add_argument('--once',action='store_true')
    try: asyncio.run(run(parser.parse_args()),loop_factory=selector_loop)
    except KeyboardInterrupt: pass
