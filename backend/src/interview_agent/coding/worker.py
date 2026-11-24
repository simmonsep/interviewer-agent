"""Controlled coding Worker; explicit live provider shares the HTTP billing ledger."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import QueueTransport, OutboxDispatcher, TaskWorker
from interview_agent.api.__main__ import selector_loop
from interview_agent.execution.docker_java import DockerProfile
from interview_agent.execution.java_workspace import JavaWorkspaceExecutor
from .repository import CodingRepository, restore
from .agent import assist, ScriptedCodingDemo, CodingFailure
from .agent import SYSTEM_PROMPT
from .billing import CodingPolicy, CodingLedger, AUTHORIZATION_ID
from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.deepseek import DeepSeekProvider, BudgetedTransport
import httpx

ROOT = Path(__file__).resolve().parents[4]


class AdmissionStopped(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)


def worker_for(repo, queue, executor=None, *, provider='disabled', solutions=None, policy=None,
               authorization=AUTHORIZATION_ID, credentials=None, inner_factory=None):
    async def prepare(claim, lost):
        row = await repo.context(claim)
        if row['candidate'] is not None: return
        if executor is None:
            await repo.fail_attempt(claim, 'coding_executor_disabled'); return
        p = row['input_payload']
        if executor.profile.image != p['policy']['image']:
            await repo.fail_attempt(claim, 'coding_image_mismatch'); return
        if row['kind'] == 'assist':
            if provider not in ('scripted', 'live'):
                await repo.fail_attempt(claim, 'coding_assistant_disabled'); return
            if p['policy'].get('assistant_provider', 'scripted') != provider:
                await repo.fail_attempt(claim, 'coding_provider_policy_mismatch'); return
        origin = ('real_model' if provider == 'live' else 'scripted_protocol') if row['kind'] == 'assist' else None
        if not await repo.begin(claim, origin): return

        async def admit(kind):
            try: await repo.admit(claim, kind)
            except ValueError as error: raise AdmissionStopped(str(error)) from None

        class Execution:
            async def grade(self, files, cases, cancel):
                await admit('execution')
                return await executor.grade(files, cases, cancel)
        runner = Execution()
        model = None
        async def failed(code, error):
            observed = getattr(getattr(model, 'transport', None), 'failure_code', None)
            await repo.fail_attempt(claim, observed or code, {'error_code': observed or code, 'exception_type': type(error).__name__,
                                                           'tool_log': error.trace if isinstance(error, CodingFailure) else []})
        try:
            if row['kind'] == 'assist':
                if provider == 'scripted':
                    model = ScriptedCodingDemo(p['task'], solutions[p['task']['id']], files=p['files'])
                else:
                    if policy is None or credentials is None: raise BillingUnavailable('model_credentials_unavailable')
                    try: key = load_key(credentials)
                    except (ValueError, TypeError): raise BillingUnavailable('model_credentials_unavailable') from None
                    model = DeepSeekProvider(key.get_secret_value(), BudgetedTransport(inner_factory() if inner_factory else httpx.AsyncHTTPTransport(retries=0),
                        CodingLedger(repo.pool, policy, authorization), claim, p), system_prompt=SYSTEM_PROMPT)
                class Provider:
                    async def complete(self, messages, tools):
                        await admit('response')
                        return await model.complete(messages, tools)
                result = {'proposal': await assist(p['task'], Provider(), runner, lost, workspace=restore(p), policy=p['policy'])}
            else:
                cases = [c for c in p['task']['cases'] if row['kind'] == 'final' or c['visibility'] == 'public']
                files = {k: v for k, v in p['files'].items() if k.endswith('.java')}
                result = {'grade': await runner.grade(files, cases, lost), 'workspace_sha256': p['files_sha256'],
                          'ability_certified': False, 'assistance_policy': p['policy']['version']}
            if not lost.is_set(): await repo.save(claim, result)
        except CodingFailure as error:
            await failed(error.code, error)
        except AdmissionStopped as error:
            await failed(error.code, error)
        except BillingUnavailable as error:
            await failed(error.code, error)
        except ValueError as error:
            safe = {'coding_claim_expired', 'coding_execution_budget_exhausted', 'coding_assistant_budget_exhausted'}
            await failed(str(error) if str(error) in safe else 'coding_candidate_invalid', error)
        except Exception as error:
            await failed('coding_provider_or_graph_error', error)
        finally:
            if model is not None and hasattr(model, 'aclose'): await model.aclose()
    return TaskWorker(repo, queue, 'coding-' + uuid4().hex, lease_seconds=6, prepare=prepare)


async def run(args):
    settings = WorkerSettings.load(args.config)
    if args.provider == 'live' and (settings.mode != 'development' or args.credentials is None):
        raise ValueError('live_requires_development_and_credentials')
    executor = JavaWorkspaceExecutor(DockerProfile.load(args.profile)) if args.executor == 'docker' else None
    if executor:
        from interview_agent.execution.docker_java import DockerJava
        await DockerJava(executor.profile).readiness()
    solutions = json.loads((ROOT / 'fixtures/ja07/scripted-solutions.json').read_text(encoding='utf-8')) if args.provider == 'scripted' else None
    pool = AsyncConnectionPool(settings.database.conninfo(), open=False, kwargs={'row_factory': dict_row}); redis = settings.redis.connect()
    try:
        await pool.open(wait=True); repo = CodingRepository(pool); queue = QueueTransport(redis, settings.stream + ':coding')
        worker = worker_for(repo, queue, executor, provider=args.provider, solutions=solutions,
                            policy=CodingPolicy.load(args.policy) if args.provider == 'live' else None,
                            authorization=args.authorization, credentials=args.credentials)
        while True:
            await OutboxDispatcher(repo, queue).dispatch(); await worker.once()
            if args.once: return
            await asyncio.sleep(.3)
    finally: await redis.aclose(); await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--executor', choices=['disabled', 'docker'], default='disabled')
    parser.add_argument('--profile', type=Path, default=Path('configs/ja06-docker-java.json'))
    parser.add_argument('--provider', choices=['disabled', 'scripted', 'live'], default='disabled')
    parser.add_argument('--policy', type=Path, default=Path('configs/ja07-deepseek-flash.json'))
    parser.add_argument('--authorization', default=AUTHORIZATION_ID); parser.add_argument('--credentials', type=Path)
    parser.add_argument('--once', action='store_true')
    try: asyncio.run(run(parser.parse_args()), loop_factory=selector_loop)
    except KeyboardInterrupt: pass
