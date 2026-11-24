"""Separate career stream; reuses the existing transport/outbox/lease worker."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.billing.config import load_key, read_model_settings
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.deepseek import BudgetedTransport,DeepSeekProvider
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport,OutboxDispatcher,TaskWorker
from .billing import AUTHORIZATION_ID,CareerLedger,CareerPolicy
from .coordinator import SYSTEM_PROMPT,ScriptedCoordinator,coordinate
from .repository import CareerRepository


def worker_for(repo,queue,*,mode='disabled',policy=None,authorization=AUTHORIZATION_ID,credentials=None,inner_factory=None,coordinator_flow='dynamic'):
    if coordinator_flow not in ('dynamic','fixed_prefetch'):raise ValueError('coordinator_flow_not_supported')
    async def prepare(claim,lost):
        if await repo.cached(claim) is not None: return  # SQL complete revalidates cached candidates.
        provider=None
        try:
            context=await repo.context(claim)
            if mode=='scripted':
                provider=ScriptedCoordinator(context,lambda:repo.admit_scripted(claim))
            elif mode=='live':
                try: key=load_key(credentials)
                except (ValueError,TypeError): raise BillingUnavailable('model_credentials_unavailable') from None
                ledger=CareerLedger(repo.pool,policy,authorization)
                inner=inner_factory() if inner_factory else httpx.AsyncHTTPTransport(retries=0)
                provider=DeepSeekProvider(key.get_secret_value(),BudgetedTransport(inner,ledger,claim,context),system_prompt=SYSTEM_PROMPT)
            else: raise BillingUnavailable('model_not_enabled')
            candidate,trace=await coordinate(context,provider,lost,lambda aid:repo.application_context(claim,aid),prefetch=coordinator_flow=='fixed_prefetch')
            await repo.save(claim,candidate,'real_model' if mode=='live' else 'scripted_protocol',trace)
        except BillingUnavailable as error:
            # Invalidates the claim; TaskWorker.complete then refuses, and durable waiting can be ACKed.
            await repo.fail_assessment(claim,waiting=True,code=error.code)
        finally:
            if provider is not None and hasattr(provider,'aclose'): await provider.aclose()
    return TaskWorker(repo,queue,f'career-{uuid4().hex}',prepare=prepare)


async def run(args):
    settings=WorkerSettings.load(args.config)
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect()
    try:
        await pool.open(wait=True)
        queue=QueueTransport(redis,settings.stream+':career'); repo=CareerRepository(pool)
        policy=CareerPolicy.load(args.policy) if args.provider=='live' else None
        if policy is not None and args.model_config is not None:
            policy=policy.with_model_settings(read_model_settings(args.model_config))
        worker=worker_for(repo,queue,mode=args.provider,
            policy=policy,
            authorization=args.authorization,credentials=args.credentials)
        while True:
            dispatched=await OutboxDispatcher(repo,queue).dispatch()
            received=await worker.once()
            if args.once:
                print(json.dumps({'dispatched':dispatched,'received':received,'provider':args.provider})); return
            await asyncio.sleep(.5)
    finally:
        await redis.aclose(); await pool.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--provider',choices=['disabled','scripted','live'],default='disabled')
    parser.add_argument('--policy',type=Path,default=Path('configs/ja01-deepseek-flash.json'))
    parser.add_argument('--credentials',type=Path)
    parser.add_argument('--model-config',type=Path)
    parser.add_argument('--authorization',default=AUTHORIZATION_ID)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if args.provider=='live' and (args.credentials is None or WorkerSettings.load(args.config).mode!='development'):
        parser.error('live requires explicit credentials and development authorization')
    try: asyncio.run(run(args),loop_factory=selector_loop)
    except KeyboardInterrupt: pass


if __name__=='__main__': main()
