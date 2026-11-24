import argparse
import asyncio
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent import observability
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport,OutboxDispatcher,TaskWorker
from interview_agent.billing.config import load_key, read_model_settings
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.deepseek import BudgetedTransport,DeepSeekProvider
from .agent import interview,ScriptedInterviewer,InterviewFailure,system_prompt_for
from .repository import InterviewRepository
from .billing import InterviewPolicy,InterviewLedger,AUTHORIZATION_ID


def worker_for(repo,queue,*,provider='disabled',policy=None,authorization=AUTHORIZATION_ID,credentials=None,inner_factory=None,tracer=None):
    tracer=tracer if tracer is not None else observability.current()
    async def prepare(claim,lost):
        if await repo.cached(claim) is not None:return
        scope=await repo.context(claim);model=None
        try:
            if provider=='scripted':model=ScriptedInterviewer(scope,lambda:repo.admit_scripted(claim))
            elif provider=='live':
                if policy is None:
                    from interview_agent.billing.config import default_policy
                    effective=default_policy()
                else:effective=policy
                try:
                    if credentials=='vault':
                        # The desktop stores the key in Windows Credential Manager; no
                        # plaintext credential file is created for the frozen app.
                        from pydantic import SecretStr
                        from interview_agent.desktop import key_vault
                        raw=key_vault.load()
                        if not raw:raise ValueError('model_credentials_unavailable')
                        key=SecretStr(raw)
                    else:key=load_key(credentials)
                except (ValueError,TypeError):raise BillingUnavailable('model_credentials_unavailable') from None
                model=DeepSeekProvider(key.get_secret_value(),BudgetedTransport(inner_factory() if inner_factory else httpx.AsyncHTTPTransport(retries=0),InterviewLedger(repo.pool,effective,authorization),claim,scope,tracer=tracer),system_prompt=system_prompt_for(scope),tracer=tracer)
            else:raise BillingUnavailable('model_not_enabled')
            result=await interview(scope,model,lost,tracer=tracer)
            if not lost.is_set():await repo.save(claim,result,'real_model' if provider=='live' else 'scripted_protocol')
        except BillingUnavailable as error:await repo.fail_assessment(claim,waiting=True,code=error.code)
        except InterviewFailure as error:await repo.fail_assessment(claim,code=error.code,diagnostic=error.diagnostic)
        finally:
            if model is not None and hasattr(model,'aclose'):await model.aclose()
    return TaskWorker(repo,queue,f'interview-{uuid4().hex}',prepare=prepare,tracer=tracer)


async def run(args):
    settings=WorkerSettings.load(args.config)
    # F07Z-62: the desktop product runs the test database, so live is no longer restricted
    # to `development`. What gates spending is the explicit authorization with a cap plus the
    # user's own key, not the database name.
    if args.provider=='live' and args.credentials is None:raise ValueError('live_requires_credentials')
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row});redis=settings.redis.connect()
    observability.configure('interview-worker',mode=settings.mode)
    observability.current().record('worker.started',stage='worker',provider=args.provider)
    try:
        await pool.open(wait=True)
        from .memory_store import MemoryProjector
        repo=InterviewRepository(pool,projector=MemoryProjector(settings.database))
        queue=QueueTransport(redis,settings.stream+':interviews')
        if args.provider=='live':
            from interview_agent.billing.config import default_policy
            policy=InterviewPolicy.load(args.policy) if args.policy else default_policy()
        else:policy=None
        if policy is not None and args.model_config is not None:
            policy=policy.with_model_settings(read_model_settings(args.model_config))
        worker=worker_for(repo,queue,provider=args.provider,policy=policy,authorization=args.authorization,credentials=args.credentials)
        while True:
            await OutboxDispatcher(repo,queue).dispatch();await worker.once()
            if args.once:return
            await asyncio.sleep(.5)
    finally:await redis.aclose();await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Whole-session interview/coaching Worker; disabled by default.')
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--provider',choices=['disabled','scripted','live'],default='disabled')
    parser.add_argument('--policy',type=Path,default=Path('configs/ja05-deepseek-flash.json'));parser.add_argument('--authorization',default=AUTHORIZATION_ID)
    parser.add_argument('--credentials',type=Path);parser.add_argument('--model-config',type=Path);parser.add_argument('--once',action='store_true')
    try:asyncio.run(run(parser.parse_args()),loop_factory=selector_loop)
    except KeyboardInterrupt:pass
