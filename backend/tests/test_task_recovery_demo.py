"""S04B: one reproducible recovery run — commit succeeded, the message was never ACKed.

This is the acceptance case, not a new production path: it uses the same API, the same
OperationRepository and the same Redis Streams consumer group as `test_worker.py`. The first
consumer commits the business result and deliberately does not acknowledge; a second consumer
reclaims the pending message. The assertions pin:

- pending goes 1 -> 0 after the reclaiming worker runs;
- both consumers refer to the same operation id;
- the target version advances exactly once (1 -> 2, not 3);
- an old lease holder still cannot commit after a new fencing token exists.

Set `S04B_EVIDENCE=<path>` to also write the four-row timeline as JSON.
"""
import json
import os
from pathlib import Path

import pytest

from interview_agent.worker.queue import OutboxDispatcher, TaskWorker, parse_message
from test_postgres import arun, pg_settings, query  # noqa: F401 (fixtures)
from test_worker import (enqueue, expire, message_for, resources, sql, task_context,  # noqa: F401
                         worker_settings)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


async def pending(queue):
    value = await queue.redis.xpending(queue.stream, queue.group)
    return (value or {}).get("pending", 0)


def target_version(client, target):
    return client.get(f"/api/training-targets/{target['id']}").json()["version"]


def test_commit_before_ack_reclaims_with_one_effect(task_context, worker_settings, pg_settings):
    client, target, user = task_context
    key = f"s04b-{os.urandom(8).hex()}"
    first = enqueue(task_context, key)
    replay = enqueue(task_context, key)
    conflict = enqueue(task_context, key, focus="different")
    assert first.status_code == 202 and replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    assert conflict.status_code == 409
    operation_id = first.json()["id"]
    assert target_version(client, target) == 1

    timeline = []

    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            assert await OutboxDispatcher(repo, queue).dispatch() == 1
            _message_id, fields = await queue.receive("first-consumer", 0)
            message = parse_message(fields)
            claim = await repo.claim(message)
            assert claim is not None and claim.message.operation_id == operation_id
            assert await repo.complete(claim)  # business result is committed here
            timeline.append({"step": "commit without ack", "operation_id": operation_id,
                             "pending": await pending(queue),
                             "target_version": (await sql(
                                 repo, "SELECT version FROM app.training_targets WHERE id=%s",
                                 (claim.resource_id,)))[0]["version"],
                             "status": (await sql(
                                 repo, "SELECT status FROM app.operations WHERE id=%s",
                                 (operation_id,)))[0]["status"]})

            # Delivery is still pending: a second consumer reclaims the same message.
            assert await TaskWorker(repo, queue, "second-consumer", reclaim_idle_ms=0).once()
            timeline.append({"step": "reclaim and ack", "operation_id": operation_id,
                             "pending": await pending(queue),
                             "target_version": (await sql(
                                 repo, "SELECT version FROM app.training_targets WHERE id=%s",
                                 (claim.resource_id,)))[0]["version"],
                             "status": (await sql(
                                 repo, "SELECT status FROM app.operations WHERE id=%s",
                                 (operation_id,)))[0]["status"]})
            assert await pending(queue) == 0
            assert (await sql(repo, "SELECT version FROM app.training_targets WHERE id=%s",
                              (claim.resource_id,)))[0]["version"] == 2

            # A second operation proves the old lease holder cannot commit after a new fence.
            second = enqueue(task_context, version=2)
            assert second.status_code == 202
            assert second.json()["id"] != operation_id  # a new operation, not the replayed one
            assert await OutboxDispatcher(repo, queue).dispatch() == 1
            _mid, fields = await queue.receive("old-consumer", 0)
            old_message = parse_message(fields)
            old = await repo.claim(old_message)
            await expire(repo, old_message)
            new = await repo.claim(old_message)
            assert new is not None and new.fencing_token > old.fencing_token
            assert not await repo.complete(old)
            assert await repo.complete(new)
            timeline.append({"step": "old fence rejected", "operation_id": old_message.operation_id,
                             "pending": await pending(queue),
                             "old_attempt": old.attempt, "new_attempt": new.attempt,
                             "old_fence": old.fencing_token, "new_fence": new.fencing_token})
            assert await TaskWorker(repo, queue, "third-consumer", reclaim_idle_ms=0).once()
            timeline.append({"step": "final readback", "operation_id": old_message.operation_id,
                             "pending": await pending(queue),
                             "target_version": (await sql(
                                 repo, "SELECT version FROM app.training_targets WHERE id=%s",
                                 (claim.resource_id,)))[0]["version"]})
            assert await pending(queue) == 0

    arun(scenario())

    # Read back through the authenticated API: same operation, one effect.
    viewed = client.get(f"/api/operations/{operation_id}").json()
    assert viewed["status"] == "SUCCEEDED"
    assert viewed["result"]["version"] == 2
    assert target_version(client, target) == 3  # first op 1->2, second op 2->3, no repeats

    path = os.environ.get("S04B_EVIDENCE")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"operation_id": operation_id,
                                          "replay_operation_id": replay.json()["id"],
                                          "conflict_status": conflict.status_code,
                                          "timeline": timeline}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
