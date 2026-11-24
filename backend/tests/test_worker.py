"""Real PG + real independent Redis; injected failures are named, not production incidents."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import AuthenticationError, ConnectionError

from interview_agent.contracts.operations import QueueMessage
from interview_agent.contracts.resources import UpdateFocus
from interview_agent.storage.operations import OperationRepository
from interview_agent.worker.config import WorkerSettings
from interview_agent.worker.queue import OutboxDispatcher, QueueTransport, TaskWorker, parse_message

from test_postgres import arun, client_for, create, pg_settings, query, signup

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture(scope="session")
def worker_settings(pg_settings):
    path = os.environ.get("PY_INTERVIEW_WORKER_TEST_CONFIG")
    if not path:
        pytest.skip("Set PY_INTERVIEW_WORKER_TEST_CONFIG to the new private test-worker.json")
    settings = WorkerSettings.load(path)
    assert settings.mode == "test" and settings.redis.db == 1 and settings.database.dbname == "py_interview_test"
    return settings


@pytest.fixture
def task_context(pg_settings, worker_settings):
    with client_for(pg_settings[0]) as client:
        client.headers["Origin"] = pg_settings[0].origin
        user = signup(client)["user"]["id"]
        target = create(client).json()
        yield client, target, user
    # Delete only rows belonging to this fixture's synthetic account, never shared databases/streams.
    async def cleanup():
        async with await AsyncConnection.connect(pg_settings[1].conninfo(), row_factory=dict_row) as conn:
            await conn.execute("DELETE FROM app.business_events WHERE user_id=%s", (user,))
            await conn.execute("DELETE FROM app.assessment_revisions WHERE user_id=%s", (user,))
            await conn.execute("DELETE FROM app.assessment_candidates c USING app.operations o WHERE c.operation_id=o.id AND o.user_id=%s", (user,))
            await conn.execute("DELETE FROM app.outbox_events e USING app.background_jobs j,app.operations o WHERE e.job_id=j.id AND j.operation_id=o.id AND o.user_id=%s", (user,))
            await conn.execute("DELETE FROM app.background_jobs j USING app.operations o WHERE j.operation_id=o.id AND o.user_id=%s", (user,))
            await conn.execute("DELETE FROM app.operations WHERE user_id=%s", (user,))
            await conn.execute("DELETE FROM app.answer_attempts WHERE user_id=%s", (user,))
            await conn.execute("DELETE FROM app.practice_sessions WHERE user_id=%s", (user,))
            await conn.execute("DELETE FROM app.training_targets WHERE user_id=%s", (user,))
            await conn.execute("DELETE FROM auth.users WHERE id=%s", (user,))
    arun(cleanup())


@asynccontextmanager
async def resources(settings):
    redis = settings.redis.connect()
    pool = AsyncConnectionPool(settings.database.conninfo(), open=False, min_size=1, max_size=8,
                               timeout=5, kwargs={"row_factory": dict_row})
    queue = QueueTransport(redis, f"py-interview:test:{uuid4().hex}:jobs:v1")
    try:
        await pool.open(wait=True, timeout=8)
        await redis.ping()
        yield OperationRepository(pool), queue
    finally:
        await redis.delete(queue.stream)
        await redis.aclose()
        await pool.close()


def enqueue(context, key=None, focus="SQL outbox", version=1):
    client, target, _ = context
    return client.post(f"/api/training-targets/{target['id']}/focus-operations", json={"expected_version": version, "focus": focus},
                       headers={"Idempotency-Key": key or f"cmd-{uuid4().hex}"})


def message_for(owner, operation):
    row = query(owner, "SELECT id FROM app.background_jobs WHERE operation_id=%s", (operation["id"],))[0]
    return QueueMessage(job_id=str(row["id"]), operation_id=operation["id"], resource_version=operation["expected_version"])


async def sql(repo, statement, params=()):
    async with repo.pool.connection() as conn:
        return await (await conn.execute(statement, params)).fetchall()


async def expire(repo, message):
    await sql(repo, "UPDATE app.background_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s RETURNING id", (message.job_id,))


def test_worker_202_is_saved_not_completed_and_replay(task_context, pg_settings):
    client, target, user = task_context
    key = f"cmd-{uuid4().hex}"
    first = enqueue(task_context, key)
    assert first.status_code == 202 and first.json()["status"] == "PENDING"
    replay = enqueue(task_context, key)
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    assert enqueue(task_context, key, focus="different").status_code == 409
    assert client.get(f"/api/training-targets/{target['id']}").json()["version"] == 1
    rows = query(pg_settings[1], "SELECT count(*) AS n FROM app.operations o JOIN app.background_jobs j ON j.operation_id=o.id JOIN app.outbox_events e ON e.job_id=j.id WHERE o.user_id=%s", (user,))
    assert rows[0]["n"] == 1


def test_worker_admission_validation_and_owner(task_context, pg_settings):
    client, target, _ = task_context
    assert enqueue(task_context, version=2).status_code == 409
    assert client.post(f"/api/training-targets/{target['id']}/focus-operations", json={"expected_version": 1, "focus": "x", "user_id": target["user_id"]}, headers={"Idempotency-Key": "valid-key-1"}).status_code == 400
    operation = enqueue(task_context).json()
    with client_for(pg_settings[0]) as other:
        other.headers["Origin"] = pg_settings[0].origin
        signup(other)
        assert other.get(f"/api/operations/{operation['id']}").status_code == 404
        assert other.post(f"/api/operations/{operation['id']}/cancel", json={}).status_code == 404
        assert other.post(f"/api/training-targets/{target['id']}/focus-operations", json={"expected_version": 1, "focus": "x"}, headers={"Idempotency-Key": "valid-key-1"}).status_code == 404


def test_worker_outbox_failure_rolls_back_entire_command(task_context, pg_settings):
    client, target, user = task_context
    async def scenario():
        async with await AsyncConnection.connect(pg_settings[1].conninfo()) as conn:
            await conn.execute("CREATE FUNCTION app.py05_reject_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic outbox insert failure'; END $$")
            await conn.execute("CREATE TRIGGER py05_reject_outbox BEFORE INSERT ON app.outbox_events FOR EACH ROW EXECUTE FUNCTION app.py05_reject_outbox()")
    arun(scenario())
    try:
        response = enqueue(task_context)
        assert response.status_code == 500 and response.json() == {"code": "internal_error"}
        assert query(pg_settings[1], "SELECT count(*) AS n FROM app.operations WHERE user_id=%s", (user,))[0]["n"] == 0
    finally:
        async def remove_probe():
            async with await AsyncConnection.connect(pg_settings[1].conninfo()) as conn:
                await conn.execute("DROP TRIGGER py05_reject_outbox ON app.outbox_events")
                await conn.execute("DROP FUNCTION app.py05_reject_outbox()")
        arun(remove_probe())


def test_worker_end_to_end_streams_and_no_private_payload(task_context, worker_settings, pg_settings):
    client, target, _ = task_context
    operation = enqueue(task_context, focus="SYNTHETIC-PRIVATE-FOCUS").json()
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            assert await OutboxDispatcher(repo, queue).dispatch() == 1
            rows = await queue.redis.xrange(queue.stream)
            fields = rows[0][1]
            assert set(fields) == {"job_id", "operation_id", "resource_version"}
            assert "SYNTHETIC-PRIVATE-FOCUS" not in json.dumps(rows)
            assert await TaskWorker(repo, queue, "worker-A").once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
    arun(scenario())
    result = client.get(f"/api/operations/{operation['id']}").json()
    assert result["status"] == "SUCCEEDED" and result["result"]["version"] == 2
    assert client.get(f"/api/training-targets/{target['id']}").json()["focus"] == "SYNTHETIC-PRIVATE-FOCUS"
    assert enqueue(task_context, focus="later", version=1).status_code == 409


def test_worker_publish_failure_retains_sql_then_recovers(task_context, worker_settings, pg_settings):
    operation = enqueue(task_context).json()
    message = message_for(pg_settings[1], operation)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            original = queue.publish
            async def fail(message):
                raise ConnectionError("synthetic transport unavailable")
            queue.publish = fail
            with pytest.raises(ConnectionError):
                await OutboxDispatcher(repo, queue).dispatch()
            row = (await sql(repo, "SELECT dispatched_at,dispatch_count FROM app.outbox_events WHERE job_id=%s", (message.job_id,)))[0]
            assert row == {"dispatched_at": None, "dispatch_count": 0}
            queue.publish = original
            assert await OutboxDispatcher(repo, queue).dispatch() == 1
            await TaskWorker(repo, queue, "recovered").once()
    arun(scenario())
    assert task_context[0].get(f"/api/operations/{operation['id']}").json()["status"] == "SUCCEEDED"


def test_worker_publish_before_sql_mark_duplicate_has_one_effect(task_context, worker_settings):
    operation = enqueue(task_context).json()
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            original = repo.mark_dispatched
            async def fail(outbox_id):
                raise RuntimeError("synthetic crash after XADD before SQL mark")
            repo.mark_dispatched = fail
            with pytest.raises(RuntimeError):
                await OutboxDispatcher(repo, queue).dispatch()
            repo.mark_dispatched = original
            await OutboxDispatcher(repo, queue).dispatch()
            assert await queue.redis.xlen(queue.stream) == 2
            worker = TaskWorker(repo, queue, "duplicate-worker")
            await worker.once()
            await worker.once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2


def test_worker_pending_reclaimed_by_another_consumer(task_context, worker_settings):
    enqueue(task_context)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            abandoned = await queue.receive("dead-consumer", 0)
            assert abandoned and (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 1
            await TaskWorker(repo, queue, "new-consumer", reclaim_idle_ms=0).once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2


def test_worker_deleted_notification_recovers_from_sql_outbox(task_context, worker_settings):
    enqueue(task_context)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            dispatcher = OutboxDispatcher(repo, queue)
            await dispatcher.dispatch()
            message_id = (await queue.redis.xrange(queue.stream))[0][0]
            await queue.redis.xdel(queue.stream, message_id)
            assert await dispatcher.dispatch(0) == 1
            await TaskWorker(repo, queue, "recover-missing").once()
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2


def test_worker_two_claimers_one_lease_and_wrong_identity_rejected(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claims = await asyncio.gather(repo.claim(message), repo.claim(message))
            assert sum(c is not None for c in claims) == 1
            claim = next(c for c in claims if c)
            assert not await repo.can_ack(message)  # An active duplicate is kept pending, not treated as success.
            assert not await repo.complete(replace(claim, user_id=str(uuid4())))
            assert not await repo.heartbeat(replace(claim, fencing_token=claim.fencing_token+1))
            assert await repo.complete(claim)
            assert await repo.can_ack(message)
    arun(scenario())


def test_worker_expired_lease_new_fence_rejects_old_completion(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            old = await repo.claim(message)
            await expire(repo, message)
            assert not await repo.complete(old) and not await repo.heartbeat(old)
            new = await repo.claim(message)
            assert new.attempt == old.attempt+1 and new.fencing_token > old.fencing_token
            assert not await repo.complete(old) and not await repo.fail_attempt(old)
            assert await repo.complete(new)
    arun(scenario())


@pytest.mark.parametrize("running", [False, True])
def test_worker_cancel_invalidates_execution(task_context, worker_settings, pg_settings, running):
    operation = enqueue(task_context).json()
    message = message_for(pg_settings[1], operation)
    async def take():
        async with resources(worker_settings) as (repo, queue):
            return await repo.claim(message)
    claim = arun(take()) if running else None
    cancelled = task_context[0].post(f"/api/operations/{operation['id']}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert task_context[0].post(f"/api/operations/{operation['id']}/cancel", json={}).json()["status"] == "CANCELLED"
    async def verify():
        async with resources(worker_settings) as (repo, queue):
            assert await repo.claim(message) is None and await repo.can_ack(message)
            if claim:
                assert not await repo.complete(claim) and not await repo.heartbeat(claim)
    arun(verify())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 1


@pytest.mark.parametrize("change", ["version", "delete"])
def test_worker_changed_resource_rejects_late_commit(task_context, worker_settings, pg_settings, change):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def take():
        async with resources(worker_settings) as (repo, queue):
            return await repo.claim(message)
    claim = arun(take())
    client, target, _ = task_context
    if change == "version":
        assert client.patch(f"/api/training-targets/{target['id']}", json={"expected_version": 1, "focus": "manual"}).status_code == 200
    else:
        assert client.request("DELETE", f"/api/training-targets/{target['id']}", json={"expected_version": 1}).status_code == 200
    async def verify():
        async with resources(worker_settings) as (repo, queue):
            assert not await repo.heartbeat(claim)
            assert not await repo.complete(claim)
            assert await repo.can_ack(message)
            assert (await sql(repo, "SELECT status,error_code FROM app.operations WHERE id=%s", (message.operation_id,)))[0] == {"status": "FAILED", "error_code": "resource_changed"}
    arun(verify())


@pytest.mark.parametrize("method", ["complete", "heartbeat"])
def test_worker_lease_rechecked_after_target_lock_wait(task_context, worker_settings, pg_settings, method):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message, 0.12)
            async with await AsyncConnection.connect(pg_settings[1].conninfo(), autocommit=True) as blocker:
                async with blocker.transaction():
                    await blocker.execute("SELECT id FROM app.training_targets WHERE id=%s FOR UPDATE", (claim.resource_id,))
                    waiting = asyncio.create_task(getattr(repo, method)(claim))
                    # Deliberately hold the resource row past the short SQL lease.
                    await asyncio.sleep(0.2)
                    assert not waiting.done()
                assert await waiting is False
            assert await repo.complete(await repo.claim(message))
    arun(scenario())


def test_worker_effect_and_terminal_state_rollback_together(task_context, worker_settings, pg_settings, monkeypatch):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            original = repo._finish
            async def fail(*args, **kwargs):
                raise RuntimeError("synthetic failure after target update")
            monkeypatch.setattr(repo, "_finish", fail)
            with pytest.raises(RuntimeError):
                await repo.complete(claim)
            row = (await sql(repo, "SELECT version,focus FROM app.training_targets WHERE id=%s", (claim.resource_id,)))[0]
            assert row == {"version": 1, "focus": ""}
            assert not await repo.can_ack(message)
            monkeypatch.setattr(repo, "_finish", original)
            assert await repo.complete(claim)
    arun(scenario())


def test_worker_commit_before_ack_can_reclaim_without_second_effect(task_context, worker_settings):
    enqueue(task_context)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            message_id, fields = await queue.receive("commit-no-ack", 0)
            claim = await repo.claim(parse_message(fields))
            assert await repo.complete(claim)
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 1
            await TaskWorker(repo, queue, "after-commit", reclaim_idle_ms=0).once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2


def test_worker_three_failures_are_bounded_and_persisted(task_context, worker_settings, pg_settings):
    operation = enqueue(task_context).json()
    message = message_for(pg_settings[1], operation)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            for attempt in range(1, 4):
                claim = await repo.claim(message)
                assert claim.attempt == attempt
                assert await repo.fail_attempt(claim)
                if attempt < 3:
                    assert await repo.claim(message) is None  # ready_at backoff is enforced.
                    await sql(repo, "UPDATE app.background_jobs SET ready_at=clock_timestamp() WHERE id=%s RETURNING id", (message.job_id,))
            assert await repo.claim(message) is None and await repo.can_ack(message)
            assert (await sql(repo, "SELECT attempt,status FROM app.background_jobs WHERE id=%s", (message.job_id,)))[0] == {"attempt": 3, "status": "FAILED"}
    arun(scenario())


def test_worker_three_abandoned_attempts_cannot_claim_fourth(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            for attempt in range(1, 4):
                assert (await repo.claim(message)).attempt == attempt
                await expire(repo, message)
            assert await repo.claim(message) is None and await repo.can_ack(message)
            assert (await sql(repo, "SELECT error_code FROM app.operations WHERE id=%s", (message.operation_id,)))[0]["error_code"] == "attempts_exhausted"
    arun(scenario())


def test_worker_runtime_renews_and_releases_failed_preparation(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            async def prepare(claim, lost):
                await asyncio.sleep(0.2)
                assert not lost.is_set()
                raise RuntimeError("synthetic deterministic preparation failure")
            worker = TaskWorker(repo, queue, "renewing", lease_seconds=0.12, prepare=prepare)
            await worker.once()
            row = (await sql(repo, "SELECT status,attempt FROM app.background_jobs WHERE id=%s", (message.job_id,)))[0]
            assert row == {"status": "PENDING", "attempt": 1}
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
    arun(scenario())


def test_worker_native_cancel_leaves_pending_for_sql_recovery(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            entered = asyncio.Event()
            async def prepare(claim, lost):
                entered.set()
                await asyncio.Event().wait()
            task = asyncio.create_task(TaskWorker(repo, queue, "cancel-task", prepare=prepare).once())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 1
            await expire(repo, message)
            await TaskWorker(repo, queue, "recover-cancel-task", reclaim_idle_ms=0).once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
    arun(scenario())


def test_worker_invalid_messages_do_not_change_resources(task_context, worker_settings):
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await queue.redis.xadd(queue.stream, {"job_id": "bad", "extra": "synthetic"})
            await TaskWorker(repo, queue, "reject-invalid").once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
            for version in ["0", "-1", "1.0", "2147483648", "99999999999"]:
                assert parse_message({"job_id": str(uuid4()), "operation_id": str(uuid4()), "resource_version": version}) is None
            assert await repo.claim(QueueMessage(job_id=str(uuid4()), operation_id=str(uuid4()), resource_version=1)) is None
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 1


def test_worker_redis_requires_password(worker_settings):
    async def scenario():
        client = worker_settings.redis.connect()
        unauthenticated = type(client)(host="127.0.0.1", port=worker_settings.redis.port, socket_timeout=2)
        try:
            with pytest.raises(AuthenticationError):
                await unauthenticated.ping()
            assert await client.ping()
        finally:
            await client.aclose()
            await unauthenticated.aclose()
    arun(scenario())


def test_worker_two_workers_duplicate_delivery_one_preparation(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await queue.publish(message)
            await queue.publish(message)
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def prepare(claim, lost):
                calls.append(claim.fencing_token)
                entered.set()
                await release.wait()
            first = asyncio.create_task(TaskWorker(repo, queue, "parallel-A", prepare=prepare).once())
            await entered.wait()
            await TaskWorker(repo, queue, "parallel-B", prepare=prepare).once()
            assert len(calls) == 1
            # Both deliveries remain pending while the only valid execution is running.
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 2
            release.set()
            await first
            await TaskWorker(repo, queue, "parallel-B", reclaim_idle_ms=0).once()
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 0
            assert len(calls) == 1
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2


def test_worker_heartbeat_loss_blocks_prepared_result(task_context, worker_settings, pg_settings):
    message = message_for(pg_settings[1], enqueue(task_context).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await queue.publish(message)
            entered = asyncio.Event()
            async def prepare(claim, lost):
                entered.set()
                await asyncio.wait_for(lost.wait(), timeout=3)
            task = asyncio.create_task(TaskWorker(repo, queue, "loses-lease", lease_seconds=0.12, prepare=prepare).once())
            await entered.wait()
            await expire(repo, message)
            await task
            row = (await sql(repo, "SELECT version FROM app.training_targets WHERE id=%s", (task_context[1]["id"],)))[0]
            assert row["version"] == 1
            assert (await queue.redis.xpending(queue.stream, queue.group))["pending"] == 1
            await TaskWorker(repo, queue, "after-lost-lease", reclaim_idle_ms=0).once()
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2


def test_worker_uncertain_xadd_disconnects_without_automatic_retry(task_context, worker_settings, monkeypatch):
    enqueue(task_context)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            original_send = queue.redis._send_command_parse_response
            original_close = queue.redis._close_connection
            sends, closes = [], []
            async def uncertain(conn, command_name, *args, **kwargs):
                result = await original_send(conn, command_name, *args, **kwargs)
                if command_name == "XADD":
                    sends.append(command_name)
                    raise ConnectionError("synthetic lost acknowledgement after actual XADD")
                return result
            async def closed(*args, **kwargs):
                closes.append(True)
                return await original_close(*args, **kwargs)
            monkeypatch.setattr(queue.redis, "_send_command_parse_response", uncertain)
            monkeypatch.setattr(queue.redis, "_close_connection", closed)
            with pytest.raises(ConnectionError):
                await OutboxDispatcher(repo, queue).dispatch()
            assert len(sends) == 1 and len(closes) == 1
            monkeypatch.setattr(queue.redis, "_send_command_parse_response", original_send)
            assert await queue.redis.xlen(queue.stream) == 1
            await OutboxDispatcher(repo, queue).dispatch()
            assert await queue.redis.xlen(queue.stream) == 2
            worker = TaskWorker(repo, queue, "uncertain-response")
            await worker.once()
            await worker.once()
    arun(scenario())
    assert task_context[0].get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 2
