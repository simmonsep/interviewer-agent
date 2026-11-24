"""Scoped synthetic Workers for desktop-shell acceptance (test environment only).

Advances ONE synthetic goal's queued work with the real code paths:
- the materials Worker (the pipeline's rewrite step), and
- the preparation Worker (graph + PostgreSQL checkpointer).

It exits once it has seen a preparation and that preparation left PENDING/RUNNING, or when the
watch window ends (the pipeline spends ~30 s on its rewrite step before a preparation exists).

Model calls: `scripted` (the default) makes none. `--provider live` is passed by the desktop
acceptance only when the user has enabled planning and supplied a capped authorization, so the
free path stays free.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from interview_agent import observability
from interview_agent.api.__main__ import selector_loop
from interview_agent.interview_prep import checkpointer
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.interview_prep.worker import worker_for as prep_worker_for
from interview_agent.materials.repository import MaterialRepository
from interview_agent.materials.worker import worker_for as materials_worker_for
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import OutboxDispatcher, QueueTransport

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    settings = WorkerSettings.load(ROOT / '.private/redis/test-worker.json')
    assert (settings.mode, settings.database.dbname, settings.redis.db) == ('test', 'py_interview_test', 1)
    pool = AsyncConnectionPool(settings.database.conninfo(), open=False, kwargs={'row_factory': dict_row})
    redis = settings.redis.connect()
    prefix = f'py-interview:test:{uuid4().hex}:jobs:v1'
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            row = await (await conn.execute(
                'SELECT g.id,u.email FROM app.career_goals g JOIN auth.users u ON u.id=g.user_id WHERE g.id=%s',
                (args.goal,))).fetchone()
        email = (row or {}).get('email') or ''
        synthetic = (email.startswith(f'pyweb-{args.run_id}-')
                     or (email.startswith('local-') and email.endswith('@desktop.example.com')))
        assert row and synthetic, 'this_run_synthetic_goal_required'

        async def preps():
            async with pool.connection() as conn:
                return await (await conn.execute(
                    'SELECT id,status FROM app.career_interview_preparations WHERE goal_id=%s ORDER BY created_at DESC',
                    (args.goal,))).fetchall()

        async def runs():
            async with pool.connection() as conn:
                return await (await conn.execute(
                    'SELECT id,status FROM app.career_material_runs WHERE goal_id=%s ORDER BY created_at DESC',
                    (args.goal,))).fetchall()

        class ScopedPreparations(PreparationRepository):
            async def pending_dispatch(self, recovery_seconds=5):
                mine = {str(item['id']) for item in await preps()}
                return [r for r in await super().pending_dispatch(recovery_seconds)
                        if str(r['operation_id']) in mine]

        class ScopedMaterials(MaterialRepository):
            async def pending_dispatch(self, recovery_seconds=5):
                mine = {str(item['id']) for item in await runs()}
                return [r for r in await super().pending_dispatch(recovery_seconds)
                        if str(r['operation_id']) in mine]

        prep_repo = ScopedPreparations(pool)
        prep_queue = QueueTransport(redis, prefix + ':preparations')
        planner_factory = None
        if args.provider == 'live':
            # Same gate as the product: live needs an explicit authorization and a key file.
            from interview_agent.interviews.billing import InterviewPolicy
            from interview_agent.interview_prep.planner import LivePlanner
            policy = InterviewPolicy.load(ROOT / 'configs/ja05-deepseek-flash.json')

            def planner_factory(claim):
                return LivePlanner(pool, prep_repo, claim, policy, args.authorization,
                                   args.credentials, tracer=observability.current())
        if args.mode == 'fail':
            # F07Z-166 acceptance: a real worker attempt that fails with a stable code, so the
            # page shows the failure path instead of a stubbed response.
            from interview_agent.interview_prep.planner import PlanningFailed

            class FailingPlanner:
                async def plan(self, frozen):
                    raise PlanningFailed('planning_crashed', {'stage': 'planning'})

            def planner_factory(claim):
                return FailingPlanner()
        prep_worker = prep_worker_for(prep_repo, prep_queue, pool=pool,
                                      saver_factory=lambda: checkpointer.saver(settings.database),
                                      planner_factory=planner_factory)
        material_repo = ScopedMaterials(pool)
        material_queue = QueueTransport(redis, prefix + ':materials')
        material_worker = materials_worker_for(material_repo, material_queue, provider='scripted')

        # Only work created after this point counts: earlier preparations from the same
        # browser session are already terminal and must not make the watcher exit early.
        if args.mode == 'orphan':
            # F07Z-166 acceptance: simulate a Worker that died while holding the row, so the next
            # run has to recover it from an expired lease instead of creating a second task.
            async with pool.connection() as conn:
                row = await (await conn.execute('''UPDATE app.career_interview_preparations
                    SET status='RUNNING',lease_until=clock_timestamp()-interval '1 second',attempt=0,
                    dispatched_at=NULL,fencing_token=fencing_token+1
                    WHERE id=(SELECT id FROM app.career_interview_preparations
                      WHERE goal_id=%s AND status IN ('PENDING','RUNNING')
                      ORDER BY created_at DESC LIMIT 1) RETURNING id,status''', (args.goal,))).fetchone()
            print(json.dumps({'orphaned': str(row['id']) if row else None,
                              'status': row['status'] if row else None}, ensure_ascii=False))
            return

        baseline = {str(item['id']) for item in await preps()}
        started = time.monotonic()
        deadline = started + args.watch
        seen = False
        work = 0
        while time.monotonic() < deadline:
            active_runs = [item for item in await runs() if item['status'] in ('PENDING', 'RUNNING')]
            if active_runs:
                await OutboxDispatcher(material_repo, material_queue).dispatch()
                if await material_worker.once():
                    work += 1
                    continue
            rows = await preps()
            fresh = [item for item in rows if str(item['id']) not in baseline]
            if fresh:
                seen = True
            active = [item for item in rows if item['status'] in ('PENDING', 'RUNNING')]
            if args.mode == 'plan':
                # Wait for the plan the browser just asked for, then stop.
                if seen and not active and not active_runs:
                    break
            elif args.mode == 'drain':
                # Fixture mode: finish material runs (for example a resume draft) and any
                # preparation already queued, without waiting for a new one.
                if not active and not active_runs:
                    break
            else:
                # `advance`: the confirmation may still be committing, so give the row a
                # grace period before declaring there is nothing left to do.
                if not active and not active_runs and time.monotonic() - started > 6:
                    break
            if active:
                await OutboxDispatcher(prep_repo, prep_queue).dispatch()
                if await prep_worker.once():
                    work += 1
                    continue
            await asyncio.sleep(0.4)
        async with pool.connection() as conn:
            detail = await (await conn.execute(
                'SELECT status,plan IS NOT NULL AS has_plan,session_id,error_code,error_detail '
                'FROM app.career_interview_preparations '
                'WHERE goal_id=%s ORDER BY created_at DESC LIMIT 1', (args.goal,))).fetchone()
        print(json.dumps({
            'work_steps': work,
            'saw_preparation': seen,
            'status': detail['status'] if detail else None,
            'has_plan': bool(detail['has_plan']) if detail else False,
            'session_id': str(detail['session_id']) if detail and detail['session_id'] else None,
            # The browser assertion prints this JSON, so a failed live run explains itself
            # without keeping a synthetic account behind for a manual DB query.
            'error_code': detail['error_code'] if detail else None,
            'error_detail': detail['error_detail'] if detail else None,
        }, ensure_ascii=False))
    finally:
        for suffix in ('preparations', 'materials'):
            await redis.delete(prefix + ':' + suffix)
        await redis.aclose()
        await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goal', required=True, type=lambda x: str(UUID(x)))
    parser.add_argument('--run-id', required=True, type=lambda x: str(UUID(x)))
    parser.add_argument('--watch', type=float, default=150.0)
    parser.add_argument('--provider', choices=['scripted', 'live'], default='scripted')
    parser.add_argument('--authorization', default='')
    parser.add_argument('--credentials', default='.private/live-credentials.txt')
    parser.add_argument('--mode', choices=['plan', 'advance', 'fail', 'drain', 'orphan'], default='plan',
                        help='plan: wait for a new plan; advance: drain the confirmation; '
                             'fail: plan with a planner that raises planning_crashed; '
                             'drain: only clear queued material/preparation work, then exit; '
                             'orphan: leave the latest preparation RUNNING with an expired lease')
    asyncio.run(run(parser.parse_args()), loop_factory=selector_loop)
