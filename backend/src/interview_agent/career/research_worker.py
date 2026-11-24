"""Explicit deterministic source executor; no LLM, no fee authorization."""
import argparse
import asyncio
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport,OutboxDispatcher,TaskWorker
from .research_repository import ResearchRepository
from .research_tools import ResearchInput,collect
from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.deepseek import BudgetedTransport,DeepSeekProvider
from .billing import CareerPolicy
from .delegation_billing import ResearchLedger,AUTHORIZATION_ID
from .research_agent import research,ScriptedResearcher,SYSTEM_PROMPT


class SourceTransport(httpx.AsyncBaseTransport):
    def __init__(self,inner,repo,claim):self.inner,self.repo,self.claim=inner,repo,claim
    async def handle_async_request(self,request):
        await self.repo.admit_source(self.claim)
        return await self.inner.handle_async_request(request)
    async def aclose(self):await self.inner.aclose()


def worker_for(repo,queue,*,inner_factory=None,origin='source_live',provider='disabled',policy=None,
               authorization=AUTHORIZATION_ID,credentials=None,model_inner_factory=None):
    if (inner_factory is None)!=(origin=='source_live'):raise ValueError('fixture_transport_requires_explicit_fixture_origin')
    async def prepare(claim,lost):
        if await repo.cached(claim) is not None:return
        context=await repo.context(claim)
        source=inner_factory() if inner_factory else httpx.AsyncHTTPTransport(retries=0)
        model=None
        if claim.kind=='research_agent':source=SourceTransport(source,repo,claim)
        async with httpx.AsyncClient(transport=source,
                                     timeout=15,follow_redirects=False,trust_env=False) as client:
            if claim.kind=='research_agent':
                try:
                    scope=await repo.agent_scope(claim)
                    if provider=='scripted':model=ScriptedResearcher(lambda:repo.admit_scripted(claim))
                    elif provider=='live':
                        try:key=load_key(credentials)
                        except (ValueError,TypeError):raise BillingUnavailable('model_credentials_unavailable') from None
                        ledger=ResearchLedger(repo.pool,policy,authorization)
                        model=DeepSeekProvider(key.get_secret_value(),BudgetedTransport(model_inner_factory() if model_inner_factory else httpx.AsyncHTTPTransport(retries=0),ledger,claim,scope),system_prompt=SYSTEM_PROMPT)
                    else:raise BillingUnavailable('model_not_enabled')
                    bundle=await research(scope,model,client,lost,origin=origin)
                    if not lost.is_set():await repo.save(claim,bundle,'real_model' if provider=='live' else 'scripted_protocol')
                except BillingUnavailable as error:await repo.fail_assessment(claim,waiting=True,code=error.code)
                finally:
                    if model is not None and hasattr(model,'aclose'):await model.aclose()
                return
            task=asyncio.create_task(collect(ResearchInput.model_validate(context['request']),context['jd_text'],client,origin=origin))
            stop=asyncio.create_task(lost.wait())
            try:
                done,_=await asyncio.wait({task,stop},return_when=asyncio.FIRST_COMPLETED)
                if stop in done:raise ValueError('research_cancelled')
                bundle=await task
                if not lost.is_set():await repo.save(claim,bundle)
            finally:
                for pending in (task,stop):
                    if not pending.done():pending.cancel()
                await asyncio.gather(task,stop,return_exceptions=True)
    return TaskWorker(repo,queue,f'research-{uuid4().hex}',prepare=prepare)


async def run(args):
    settings=WorkerSettings.load(args.config)
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row})
    redis=settings.redis.connect()
    try:
        await pool.open(wait=True)
        queue=QueueTransport(redis,settings.stream+':research');repo=ResearchRepository(pool)
        worker=worker_for(repo,queue,provider=args.provider,policy=CareerPolicy.load(args.policy) if args.provider=='live' else None,
                          authorization=args.authorization,credentials=args.credentials)
        while True:
            await OutboxDispatcher(repo,queue).dispatch()
            await worker.once()
            if args.once:return
            await asyncio.sleep(.5)
    finally:
        await redis.aclose();await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--provider',choices=['disabled','scripted','live'],default='disabled')
    parser.add_argument('--policy',type=Path,default=Path('configs/ja03-deepseek-flash.json'))
    parser.add_argument('--authorization',default=AUTHORIZATION_ID)
    parser.add_argument('--credentials',type=Path)
    args=parser.parse_args()
    if args.provider=='live' and (args.credentials is None or WorkerSettings.load(args.config).mode!='development'):parser.error('live requires explicit credentials and development authorization')
    try:asyncio.run(run(args),loop_factory=selector_loop)
    except KeyboardInterrupt:pass
