"""Run the materials role with the existing SQL/Redis task harness; disabled by default."""
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
from interview_agent.billing.config import load_key, read_model_settings
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.deepseek import BudgetedTransport,DeepSeekProvider
from .repository import MaterialRepository
from .billing import MaterialPolicy,MaterialLedger,AUTHORIZATION_ID,default_material_policy
from .agent import ScriptedMaterials,run_material_agent,SYSTEM_PROMPT,MaterialAgentFailure


def worker_for(repo,queue,*,provider='disabled',policy=None,authorization=AUTHORIZATION_ID,credentials=None,inner_factory=None,system_prompt=SYSTEM_PROMPT):
    """`system_prompt` defaults to the production prompt.

    The evaluation harness passes a candidate prompt to compare it against this baseline;
    production callers must not pass anything, so the shipped behaviour is unchanged.
    """
    async def prepare(claim,lost):
        if await repo.cached(claim) is not None:return
        scope=await repo.context(claim);model=None
        try:
            if provider=='scripted':model=ScriptedMaterials(scope,lambda:repo.admit_scripted(claim))
            elif provider=='live':
                # F07Z-112: same desktop adaptations as interviews/worker.py. The frozen app
                # passes no --policy and uses `--credentials vault`, so the policy falls back
                # to the embedded ja04 copy and the key comes from the Windows vault.
                effective=policy if policy is not None else default_material_policy()
                try:
                    if credentials=='vault':
                        from pydantic import SecretStr
                        from interview_agent.desktop import key_vault
                        raw=key_vault.load()
                        if not raw:raise ValueError('model_credentials_unavailable')
                        key=SecretStr(raw)
                    else:key=load_key(credentials)
                except (ValueError,TypeError):raise BillingUnavailable('model_credentials_unavailable') from None
                model=DeepSeekProvider(key.get_secret_value(),BudgetedTransport(inner_factory() if inner_factory else httpx.AsyncHTTPTransport(retries=0),MaterialLedger(repo.pool,effective,authorization),claim,scope),system_prompt=system_prompt)
            else:raise BillingUnavailable('model_not_enabled')
            result=await run_material_agent(scope,model,lost)
            if not lost.is_set():await repo.save(claim,result,'real_model' if provider=='live' else 'scripted_protocol')
        except BillingUnavailable as error:await repo.fail_assessment(claim,waiting=True,code=error.code,diagnostic={'limit':error.limit} if error.limit else None)
        except MaterialAgentFailure as error:await repo.fail_assessment(claim,code=error.code,diagnostic=error.diagnostic)
        except Exception as error:
            # A bare `material_failed` cannot be diagnosed (F07Z-46). Record the exception
            # type and, for validation errors, only the field paths — never the values.
            detail={'exception_type':type(error).__name__}
            fields=[getattr(item,'loc',None) for item in getattr(error,'errors',lambda:[])()]
            if fields:detail['fields']=[list(map(str,loc)) for loc in fields if loc]
            await repo.fail_assessment(claim,code='material_failed',diagnostic=detail)
        finally:
            if model is not None and hasattr(model,'aclose'):await model.aclose()
    return TaskWorker(repo,queue,f'material-{uuid4().hex}',prepare=prepare)


async def run(args):
    settings=WorkerSettings.load(args.config)
    # F07Z-112: the desktop product runs the test database; what gates spending is the
    # explicit capped authorization plus the user's own key, not the database name.
    if args.provider=='live' and args.credentials is None:raise ValueError('live_requires_credentials')
    pool=AsyncConnectionPool(settings.database.conninfo(),open=False,kwargs={'row_factory':dict_row});redis=settings.redis.connect()
    try:
        await pool.open(wait=True)
        queue=QueueTransport(redis,settings.stream+':materials');repo=MaterialRepository(pool)
        policy=(MaterialPolicy.load(args.policy) if args.policy else default_material_policy()) if args.provider=='live' else None
        if policy is not None and args.model_config is not None:
            policy=policy.with_model_settings(read_model_settings(args.model_config))
        worker=worker_for(repo,queue,provider=args.provider,policy=policy,authorization=args.authorization,credentials=args.credentials)
        while True:
            await OutboxDispatcher(repo,queue).dispatch();await worker.once()
            if args.once:return
            await asyncio.sleep(.5)
    finally:
        await redis.aclose();await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--provider',choices=['disabled','scripted','live'],default='disabled')
    parser.add_argument('--policy',type=Path,default=Path('configs/ja04-deepseek-flash.json'))
    parser.add_argument('--authorization',default=AUTHORIZATION_ID)
    parser.add_argument('--credentials',type=Path)
    parser.add_argument('--model-config',type=Path)
    parser.add_argument('--once',action='store_true')
    try:asyncio.run(run(parser.parse_args()),loop_factory=selector_loop)
    except KeyboardInterrupt:pass
