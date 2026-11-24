"""Preparation Worker: same SQL claim/lease/fence + Redis wake-up as the interview Worker.

`prepare` runs the graph against the official PostgreSQL checkpointer. A retry after a crash
resumes the same thread; if the thread is already parked at the interrupt it only re-syncs the
business status, it does not run the graph again.
"""
import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from interview_agent import observability
from interview_agent.api.__main__ import selector_loop
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.types import ScopeError
from interview_agent.storage.errors import ApiError
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import OutboxDispatcher, QueueTransport, TaskWorker

from . import checkpointer
from .graph import build
from .planner import PlanningFailed
from .repository import PreparationNotConfirmed, PreparationRepository

STALE_CODES = {'version_conflict', 'draft_stale', 'context_changed', 'wiki_source_withdrawn'}


def worker_for(repo, queue, *, pool, saver_factory, tracer=None, planner_factory=None):
    tracer = tracer if tracer is not None else observability.current()

    async def prepare(claim, lost):
        from langgraph.types import Command

        prep_id = claim.message.operation_id
        config = {'configurable': {'thread_id': prep_id}}
        planner = planner_factory(claim) if planner_factory is not None else None
        try:
            async with saver_factory() as saver:
                compiled = build(pool, repo, claim, tracer, planner).compile(checkpointer=saver)
                snapshot = await compiled.aget_state(config)
                row = await repo.context(claim)
                parked = bool(snapshot.next) and any(task.interrupts for task in snapshot.tasks)
                if parked and not row['decision']:
                    # The checkpoint already holds the plan; only the business row is behind.
                    await repo.mark_waiting(claim, snapshot.values.get('plan'))
                    return
                if row['decision']:
                    result = await compiled.ainvoke(
                        Command(resume={'decision': row['decision']['decision']}), config, durability='sync')
                else:
                    result = await compiled.ainvoke({'prep_id': prep_id}, config, durability='sync')
            if result.get('__interrupt__'):
                await repo.mark_waiting(claim, result.get('plan') or snapshot.values.get('plan'))
            else:
                await repo.start_session(claim, result.get('session_id'))
        except BillingUnavailable as error:
            # Admission refused (authorization/budget/UNKNOWN). Keep the stable code instead of
            # letting it degrade to `worker_execution_failed`.
            await repo.fail(claim, error.code, status='FAILED', detail={'limit': error.limit})
        except ApiError as error:
            await repo.fail(claim, error.code, status='STALE' if error.code in STALE_CODES else 'FAILED',
                            detail={'http_status': error.status})
        except PreparationNotConfirmed as error:
            await repo.fail(claim, str(error), status='FAILED')
        except PlanningFailed as error:
            await repo.fail(claim, error.code, status='FAILED', detail=error.detail)
        except ScopeError as error:
            # The provider answered, but the answer could not be used (`invalid_provider_reasoning`,
            # `invalid_provider_tool_calls`, ...). Keep that code instead of degrading to
            # `planning_crashed`; ScopeError is a ValueError, so this must come first.
            await repo.fail(claim, error.code, status='FAILED', detail={'stage': 'planning'})
        except ValueError as error:
            if str(error) != 'preparation_claim_expired':
                # A visible, stable code beats the generic `worker_execution_failed`; the class
                # name is metadata, the message may contain private material so it is dropped.
                await repo.fail(claim, 'planning_crashed', status='FAILED',
                                detail={'exception': type(error).__name__})
            # The lease was lost while running: another attempt owns this row now. Leave it.
            return
        except Exception as error:
            await repo.fail(claim, 'planning_crashed', status='FAILED',
                            detail={'exception': type(error).__name__})
        finally:
            if planner is not None and hasattr(planner, 'aclose'):
                await planner.aclose()

    return TaskWorker(repo, queue, f'prep-{uuid4().hex}', prepare=prepare, tracer=tracer)


async def run(args):
    settings = WorkerSettings.load(args.config)
    observability.configure('prep-worker', mode=settings.mode)
    pool = AsyncConnectionPool(settings.database.conninfo(), open=False, kwargs={'row_factory': dict_row})
    redis = settings.redis.connect()
    try:
        await pool.open(wait=True)
        repo = PreparationRepository(pool)
        queue = QueueTransport(redis, settings.stream + ':preparations')
        planner_factory = None
        if args.provider == 'live':
            # Same gate as the other roles: live needs an explicit authorization and credentials.
            # `scripted` means the deterministic plan, so the shipped default never spends.
            if args.credentials is None:
                raise ValueError('live_requires_credentials')
            from interview_agent.billing.config import default_policy, read_model_settings
            from interview_agent.connectors.public_reference import CONNECTOR_ID, PreparationReference
            from interview_agent.connectors.interview_experiences import (
                CONNECTOR_ID as EXPERIENCE_CONNECTOR, ExperienceLookup,
            )
            from interview_agent.connectors import experience_settings
            from interview_agent.desktop.agent_policy import _read, policy_path
            from interview_agent.interviews.billing import InterviewPolicy
            policy = InterviewPolicy.load(args.policy) if args.policy else default_policy()
            if args.model_config is not None:
                policy = policy.with_model_settings(read_model_settings(args.model_config))
            from .planner import LivePlanner
            search_path = experience_settings.settings_path(policy_path(args.model_config))

            def connector_enabled(connector):
                return _read(policy_path(args.model_config)).get('connectors', {}).get(connector) is True

            def planner_factory(claim):
                lookup = (PreparationReference(lambda: connector_enabled(CONNECTOR_ID))
                          if connector_enabled(CONNECTOR_ID) else None)
                experiences = None
                if connector_enabled(EXPERIENCE_CONNECTOR) and (service := experience_settings.selected_service(search_path)):
                    experiences = ExperienceLookup(
                        lambda: connector_enabled(EXPERIENCE_CONNECTOR)
                        and experience_settings.selected_service(search_path) == service,
                        experience_settings.credential(service), service=service, semantic_passages=True)
                return LivePlanner(pool, repo, claim, policy, args.authorization, args.credentials,
                                   reference_lookup=lookup, experience_lookup=experiences)
        worker = worker_for(repo, queue, pool=pool, saver_factory=lambda: checkpointer.saver(settings.database),
                            planner_factory=planner_factory)
        while True:
            await OutboxDispatcher(repo, queue).dispatch()
            await worker.once()
            if args.once:
                return
            await asyncio.sleep(0.5)
    finally:
        await redis.aclose()
        await pool.close()


if __name__ == '__main__':
    from interview_agent.interviews.billing import AUTHORIZATION_ID
    parser = argparse.ArgumentParser(description='Interview preparation Worker; runs the plan/review graph.')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--provider', choices=['scripted', 'live'], default='scripted',
                        help='scripted = deterministic plan (0 yuan); live = planning agent through the ledger')
    parser.add_argument('--policy', type=Path, default=None)
    parser.add_argument('--authorization', default=AUTHORIZATION_ID)
    parser.add_argument('--credentials', default=None, help="'vault' or a key file; required for live")
    parser.add_argument('--model-config', type=Path, default=None)
    parser.add_argument('--once', action='store_true')
    try:
        asyncio.run(run(parser.parse_args()), loop_factory=selector_loop)
    except KeyboardInterrupt:
        pass
