"""D-00 frozen executable entry; no credentials or configuration bundled."""
import argparse
import asyncio
import json
import sys
from uuid import UUID, uuid4
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from interview_agent.api.__main__ import selector_loop


async def check_dependencies(api_path, worker_path, allow_live=False):
    """Read-only authenticated checks; never print config values or exception text."""
    from interview_agent.storage.config import Settings
    from interview_agent.worker.config import WorkerSettings
    from psycopg import AsyncConnection
    try:
        api, worker = Settings.load(api_path), WorkerSettings.load(worker_path)
        if api.mode != 'test' or worker.mode != 'test' or api.database != worker.database:
            return {'ready': False, 'code': 'configuration_invalid'}
        # F07Z-62: live is allowed only when the host explicitly asks for it (the user turned
        # it on in settings); the shipped default still has to look scripted.
        if not allow_live and (api.assessment_mode != 'scripted' or worker.assessment_mode != 'scripted'):
            return {'ready': False, 'code': 'configuration_invalid'}
        if allow_live and api.assessment_mode not in ('scripted', 'live'):
            return {'ready': False, 'code': 'configuration_invalid'}
    except Exception:
        return {'ready': False, 'code': 'configuration_invalid'}
    try:
        async with await AsyncConnection.connect(api.database.conninfo()) as connection:
            await connection.execute('SELECT 1')
    except Exception:
        return {'ready': False, 'code': 'postgres_unavailable'}
    redis = worker.redis.connect()
    try:
        await redis.ping()
    except Exception:
        return {'ready': False, 'code': 'redis_unavailable'}
    finally:
        await redis.aclose()
    return {'ready': True, 'code': 'ready'}


async def pump(config, goal):
    from interview_agent.worker.config import WorkerSettings
    from interview_agent.worker.queue import QueueTransport
    from interview_agent.contracts.operations import QueueMessage
    from interview_agent.career.repository import CareerRepository
    from interview_agent.career.worker import worker_for
    settings = WorkerSettings.load(config)
    # Initial desktop validation only: no production jobs or live provider.
    if settings.mode != 'test':
        raise ValueError('desktop_probe_requires_test_environment')
    UUID(goal)
    pool = AsyncConnectionPool(settings.database.conninfo(), open=False, kwargs={'row_factory': dict_row})
    redis = settings.redis.connect()
    stream = 'py-interview:test:' + uuid4().hex + ':jobs:v1'
    try:
        await pool.open(wait=True, timeout=8)
        queue = QueueTransport(redis, stream)
        repo = CareerRepository(pool)
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT id,job_id,goal_version FROM app.career_steps WHERE goal_id=%s ORDER BY goal_version DESC LIMIT 1', (goal,))).fetchone()
        if row is None:
            raise ValueError('owned_probe_task_missing')
        await queue.publish(QueueMessage(operation_id=str(row['id']), job_id=str(row['job_id']), resource_version=row['goal_version']))
        if not await worker_for(repo, queue, mode='scripted').once():
            raise RuntimeError('probe_task_not_received')
    finally:
        await redis.delete(stream)
        await redis.aclose()
        await pool.close()


def serve(config, static_dir=None, model_config=None, assessment_mode=None, material_live=False):
    """Run the API. ``static_dir`` also serves the built desktop frontend from the same
    origin, which is what keeps Origin validation and the session cookie unchanged.
    ``model_config`` is the external file where the user's model access is stored.
    ``assessment_mode`` is set by the host only when the user explicitly enabled live.
    ``material_live`` reports the materials worker's mode; it does not change interview
    behaviour."""
    import uvicorn
    from interview_agent.api.app import create_app
    from interview_agent.storage.config import Settings
    settings = Settings.load(config)
    if assessment_mode:
        settings = settings.model_copy(update={'assessment_mode': assessment_mode})
    if material_live:
        settings = settings.model_copy(update={'material_live': True})
    uvicorn.run(create_app(settings, static_dir=static_dir, model_config_path=model_config),
                host='127.0.0.1', port=3100, loop='interview_agent.api.__main__:selector_loop', proxy_headers=False)


def worker(kind, config, provider, authorization=None, credentials=None, policy=None, model_config=None):
    """Run one of the first-version Workers in-process.

    F07Z-62: live is an explicit desktop choice. When it is on, Rust passes the authorization
    id (with its cap) and `--credentials vault`, so the key comes from Windows Credential
    Manager rather than a plaintext file.
    """
    from types import SimpleNamespace
    args = SimpleNamespace(config=config, provider=provider, policy=policy, authorization=authorization,
                           credentials=credentials, once=False, model_config=model_config)
    if kind == 'interview':
        from interview_agent.interviews import worker as target
    elif kind == 'interview-prep':
        # S01: the preparation Worker runs the plan/review graph with the PostgreSQL
        # checkpointer. It is always scripted: planning is deterministic and spends nothing.
        from interview_agent.interview_prep import worker as target
    elif kind == 'career':
        from interview_agent.career import worker as target
    else:
        from interview_agent.materials import worker as target
    asyncio.run(target.run(args), loop_factory=selector_loop)


def main():
    # The frozen build has a single executable: file parsing re-enters here as a child process
    # (see materials/file_repository.py:parser_command) instead of a second python.exe.
    if sys.argv[1:2] == ['file-parser']:
        from interview_agent.materials.file_parser import main as parse_document
        sys.argv = [sys.argv[0], sys.argv[2] if len(sys.argv) > 2 else 'pdf']
        parse_document()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    api = commands.add_parser('serve')
    api.add_argument('--config', required=True)
    api.add_argument('--static-dir', help='Serve this built frontend at / in the same process')
    api.add_argument('--model-config', help='External file holding the user\'s model access settings')
    api.add_argument('--assessment-mode', choices=['scripted', 'live'], default=None,
                     help='Set by the host only when the user explicitly enabled live model calls')
    api.add_argument('--material-live', action='store_true',
                     help='The host started the materials worker live; reports it, changes nothing')
    check = commands.add_parser('check-dependencies')
    check.add_argument('--api-config', required=True)
    check.add_argument('--worker-config', required=True)
    check.add_argument('--allow-live', action='store_true')
    worker_cmd = commands.add_parser('worker')
    worker_cmd.add_argument('--config', required=True)
    worker_cmd.add_argument('--kind', choices=['interview', 'interview-prep', 'materials', 'career'], required=True)
    worker_cmd.add_argument('--provider', choices=['disabled', 'scripted', 'live'], default='scripted')
    # F07Z-62: live is explicit and capped; `vault` means Windows Credential Manager.
    worker_cmd.add_argument('--authorization')
    worker_cmd.add_argument('--credentials')
    worker_cmd.add_argument('--policy')
    worker_cmd.add_argument('--model-config')
    # S00R/S04: the frozen build cannot run `python -m ...`, so the MCP catalog server is a
    # subcommand of the one executable. Only stdio is used; the host starts it as a child.
    commands.add_parser('mcp-catalog')
    probe = commands.add_parser('probe-worker')
    probe.add_argument('--config', required=True)
    probe.add_argument('--goal-id', required=True)
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.config, args.static_dir, args.model_config, args.assessment_mode, args.material_live)
    elif args.command == 'check-dependencies':
        print(json.dumps(asyncio.run(check_dependencies(args.api_config, args.worker_config, args.allow_live), loop_factory=selector_loop)))
    elif args.command == 'worker':
        worker(args.kind, args.config, args.provider, args.authorization, args.credentials,
               args.policy, args.model_config)
    elif args.command == 'mcp-catalog':
        from interview_agent.interview_prep.mcp_catalog import main as serve_catalog
        serve_catalog()
    else:
        asyncio.run(pump(args.config, args.goal_id), loop_factory=selector_loop)


if __name__ == '__main__':
    main()
