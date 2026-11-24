"""Committed SSE, cursor replay and mid-stream auth checks through real SQL."""

import asyncio
import json

import pytest

from interview_agent.api.events import operation_stream
from interview_agent.storage.auth import AuthRepository
from test_postgres import arun, client_for, pg_settings, signup
from test_training import practice, submit
from test_worker import resources, sql, task_context, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def records(body):
    result = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            result.append((fields["event"], fields.get("id"), json.loads(fields["data"])))
    return result


def test_sse_committed_replay_header_precedence_and_settled_close(task_context):
    client = task_context[0]
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    path = f"/api/operations/{operation['id']}"
    client.post(path + "/cancel", json={})
    log = client.get(path + "/event-log").json()["items"]
    response = client.get(path + "/events")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no" and "no-store" in response.headers["cache-control"]
    events = records(response.text)
    assert [data["status"] for kind, _, data in events if kind == "operation"] == ["PENDING", "CANCELLED"]
    assert [key for kind, key, _ in events if kind == "operation"] == [row["event_id"] for row in log]
    assert events[-1] == ("stream-end", None, {"reason": "settled"})
    assert "我的原回答" not in response.text and "token" not in response.text
    replay = records(client.get(path + "/events?after=0", headers={"Last-Event-ID": log[0]["event_id"]}).text)
    assert [data["status"] for kind, _, data in replay if kind == "operation"] == ["CANCELLED"]
    empty = records(client.get(path + f"/events?after={log[-1]['event_id']}").text)
    assert empty == [("stream-end", None, {"reason": "settled"})]


@pytest.mark.parametrize("cursor", ["-1", "1.5", "x", "9223372036854775808"])
def test_sse_invalid_reconnect_cursor_is_http_400(task_context, cursor):
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    response = task_context[0].get(f"/api/operations/{operation['id']}/events", headers={"Last-Event-ID": cursor})
    assert response.status_code == 400 and response.json()["code"] == "invalid_event_cursor"


def test_sse_foreign_deleted_and_logged_out_preflight(task_context, pg_settings):
    client = task_context[0]
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    path = f"/api/operations/{operation['id']}/events"
    with client_for(pg_settings[0]) as other:
        other.headers["Origin"] = pg_settings[0].origin
        signup(other)
        assert other.get(path).status_code == 404
    client.request("DELETE", f"/api/practice-sessions/{session['id']}", json={"expected_version": 2})
    assert client.get(path).status_code == 404
    client.post("/api/auth/sign-out")
    assert client.get(path).status_code == 401


def test_sse_rechecks_revocation_without_holding_a_connection(task_context, worker_settings):
    operation = submit(task_context, practice(task_context).json()).json()
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            auth = AuthRepository(repo.pool, worker_settings.session_ttl_seconds)
            token = task_context[0].cookies.get("py_interview_session")
            user = (await auth.resolve(token))[0]
            stream = operation_stream(repo, user, operation["id"], 0, interval=0)
            assert await anext(stream) == "retry: 1000\n\n"
            assert '"status":"PENDING"' in await anext(stream)
            assert repo.pool.get_stats()["pool_available"] >= 1
            await auth.logout(token)
            remaining = "".join([part async for part in stream])
            assert records(remaining) == [("auth-expired", None, {"code": "authentication_required"})]
    arun(scenario())


def test_sse_deletion_during_stream_and_bounded_disconnect_cleanup(task_context, worker_settings):
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            user = (await AuthRepository(repo.pool, worker_settings.session_ttl_seconds).resolve(task_context[0].cookies.get("py_interview_session")))[0]
            stream = operation_stream(repo, user, operation["id"], 0, interval=0, lifetime=0)
            parts = [part async for part in stream]
            assert [name for name, _, _ in records("".join(parts))] == ["operation"]
            # Disconnection cancels/closes the generator; it has no owned background task or checked-out DB connection.
            stream = operation_stream(repo, user, operation["id"], 0)
            await anext(stream)
            await anext(stream)
            assert await anext(stream) == ": keep-alive\n\n"
            await stream.aclose()
            assert repo.pool.get_stats()["pool_available"] >= 1
            await sql(repo, "UPDATE app.practice_sessions SET deleted_at=clock_timestamp() WHERE id=%s RETURNING id", (session["id"],))
            denied = "".join([part async for part in operation_stream(repo, user, operation["id"], 0)])
            assert records(denied) == [("unavailable", None, {"code": "resource_unavailable"})]
    arun(scenario())
