"""Deterministic real LangGraph through real SQL/Streams; no model or browser requests."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection

from interview_agent.contracts.practice import SubmitAnswer
from interview_agent.domain.practice import AssessmentOutput, build_snapshot, context_hash
from interview_agent.runtime.adapter import InterviewRuntimeAdapter
from interview_agent.runtime.scripted import ScriptedProvider, candidate_for, success_steps
from interview_agent.runtime.store import ScopedAssessmentStore
from interview_agent.runtime.types import ScopeError
from interview_agent.storage.auth import AuthRepository
from interview_agent.storage.errors import ApiError
from interview_agent.storage.training import TrainingRepository
from interview_agent.worker.assessment import scripted_assessment
from interview_agent.worker.queue import OutboxDispatcher, TaskWorker

from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import expire, message_for, resources, sql, task_context, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]
ANSWER = "  我的原回答\r\n\r\n工具结果需要校验。  "


def practice(context, key=None):
    client, target, _ = context
    question = client.get("/api/catalog/questions").json()["items"][0]
    response = client.post("/api/practice-sessions", json={"target_id": target["id"], "question_id": question["id"]},
                           headers={"Idempotency-Key": key or f"session-{uuid4().hex}"})
    assert response.status_code in (200, 201), response.text
    return response


def submit(context, session, *, key=None, text=ANSWER, version=1):
    return context[0].post(f"/api/practice-sessions/{session['id']}/answers", json={"expected_version": version, "text": text},
                           headers={"Idempotency-Key": key or f"answer-{uuid4().hex}"})


def test_training_waiting_session_catalog_projection_and_idempotency(task_context):
    client = task_context[0]
    key = f"session-{uuid4().hex}"
    first = practice(task_context, key)
    replay = practice(task_context, key)
    assert first.status_code == 201 and replay.status_code == 200 and first.json() == replay.json()
    value = first.json()
    assert value["status"] == "WAITING_FOR_ANSWER" and value["version"] == 1
    assert value["answer"] is value["feedback"] is value["operation"] is None
    assert "rubric" not in first.text and "sources" not in value["question"]
    assert value["question"]["semantic_scoring_eligible"] is False
    assert len(client.get("/api/catalog/questions").json()["items"]) == 8
    assert client.get("/api/practice-sessions").json()["items"][0] == value
    assert client.post("/api/practice-sessions", json={"target_id": task_context[1]["id"], "question_id": "holdout-or-unknown"}, headers={"Idempotency-Key": key}).status_code == 400


def test_training_snapshot_is_frozen_when_catalog_object_changes(task_context):
    client = task_context[0]
    original = practice(task_context).json()
    client.app.state.questions = [q.model_copy(update={"text": "SYNTHETIC changed catalog"}) for q in client.app.state.questions]
    assert client.get(f"/api/practice-sessions/{original['id']}").json()["question"] == original["question"]
    assert practice(task_context).json()["question"]["text"] == "SYNTHETIC changed catalog"


def test_training_answer_immutable_original_and_replay(task_context, pg_settings):
    client = task_context[0]
    session = practice(task_context).json()
    key = f"answer-{uuid4().hex}"
    first = submit(task_context, session, key=key)
    assert first.status_code == 202
    assert submit(task_context, session, key=key).json()["id"] == first.json()["id"]
    assert submit(task_context, session, key=key, text="覆盖").status_code == 409
    assert submit(task_context, session).status_code == 409
    saved = client.get(f"/api/practice-sessions/{session['id']}").json()
    assert saved["version"] == 2 and saved["status"] == "EVALUATING"
    assert saved["answer"]["text"] == ANSWER and saved["answer"]["origin"] == "user_submitted"
    with pytest.raises(psycopg.errors.CheckViolation, match="original_answer_immutable"):
        query(pg_settings[1], "UPDATE app.answer_attempts SET original_text='overwritten' WHERE session_id=%s RETURNING id", (session["id"],))


def test_training_submit_transaction_rolls_back_on_event_failure(task_context, pg_settings, monkeypatch):
    import interview_agent.storage.training as module
    session = practice(task_context).json()
    async def fail(*args):
        raise RuntimeError("SYNTHETIC event write failure")
    monkeypatch.setattr(module, "append_event", fail)
    assert submit(task_context, session).status_code == 500
    row = query(pg_settings[1], "SELECT status,version FROM app.practice_sessions WHERE id=%s", (session["id"],))[0]
    assert row == {"status": "WAITING_FOR_ANSWER", "version": 1}
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.answer_attempts WHERE session_id=%s", (session["id"],))[0]["n"] == 0
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.operations WHERE session_id=%s", (session["id"],))[0]["n"] == 0


def test_training_concurrent_same_answer_one_operation(task_context, worker_settings, pg_settings):
    session = practice(task_context).json()
    key = f"answer-{uuid4().hex}"
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            principal = (await AuthRepository(repo.pool, worker_settings.session_ttl_seconds).resolve(task_context[0].cookies.get("py_interview_session")))[0]
            training = TrainingRepository(repo.pool)
            value = SubmitAnswer(expected_version=1, text=ANSWER)
            results = await asyncio.gather(training.submit(principal, session["id"], key, value), training.submit(principal, session["id"], key, value))
            assert results[0].id == results[1].id
    arun(scenario())
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.operations WHERE session_id=%s", (session["id"],))[0]["n"] == 1
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.answer_attempts WHERE session_id=%s", (session["id"],))[0]["n"] == 1


def test_training_cross_session_command_key_conflict_rolls_back_second_answer(task_context, pg_settings):
    a, b = practice(task_context).json(), practice(task_context).json()
    key = f"answer-{uuid4().hex}"
    assert submit(task_context, a, key=key).status_code == 202
    assert submit(task_context, b, key=key).status_code == 409
    assert task_context[0].get(f"/api/practice-sessions/{b['id']}").json()["status"] == "WAITING_FOR_ANSWER"
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.answer_attempts WHERE session_id=%s", (b["id"],))[0]["n"] == 0


@pytest.mark.parametrize("text", ["  \r\n ", "x\n" * 40 + "x"])
def test_training_rejects_invalid_answer(task_context, text):
    session = practice(task_context).json()
    assert submit(task_context, session, text=text).status_code == 400


def test_training_owner_and_revoked_cookie_cannot_read_or_mutate(task_context, pg_settings):
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    with client_for(pg_settings[0]) as other:
        other.headers["Origin"] = pg_settings[0].origin
        signup(other)
        assert other.get("/api/practice-sessions").json()["items"] == []
        assert other.get(f"/api/practice-sessions/{session['id']}").status_code == 404
        assert other.post(f"/api/practice-sessions/{session['id']}/answers", json={"expected_version": 1, "text": "other"}, headers={"Idempotency-Key": "valid-other-key"}).status_code == 404
        assert other.request("DELETE", f"/api/practice-sessions/{session['id']}", json={"expected_version": 2}).status_code == 404
        for path in ["", "/event-log"]:
            assert other.get(f"/api/operations/{operation['id']}{path}").status_code == 404
        for path in ["resume", "cancel"]:
            assert other.post(f"/api/operations/{operation['id']}/{path}", json={}).status_code == 404
    client = task_context[0]
    cookie = client.cookies.get("py_interview_session")
    assert client.post("/api/auth/sign-out").status_code == 200
    assert client.get(f"/api/practice-sessions/{session['id']}", headers={"Cookie": f"py_interview_session={cookie}"}).status_code == 401


def test_training_real_graph_feedback_and_committed_events(task_context, worker_settings, pg_settings):
    client = task_context[0]
    session = practice(task_context).json()
    key = f"answer-{uuid4().hex}"
    operation = submit(task_context, session, key=key).json()
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            await TaskWorker(repo, queue, "assess-success", assess=scripted_assessment).once()
    arun(scenario())
    saved = client.get(f"/api/practice-sessions/{session['id']}").json()
    assert saved["status"] == "COMPLETED" and saved["version"] == 3 and saved["answer"]["text"] == ANSWER
    assert saved["feedback"]["origin"] == "scripted_protocol" and saved["feedback"]["provider"] == "pm003-scripted"
    assert saved["feedback"]["semantic_scoring_eligible"] is False and "verdict" not in saved["feedback"]
    assert {c["kind"] for c in saved["feedback"]["citations"]} == {"evidence", "answer"}
    assert next(c for c in saved["feedback"]["citations"] if c["kind"] == "answer")["quote"] == ANSWER.replace("\r\n", "\n")
    events = client.get(f"/api/operations/{operation['id']}/event-log").json()
    assert [e["status"] for e in events["items"]] == ["PENDING", "RUNNING", "SUCCEEDED"]
    assert [int(e["event_id"]) for e in events["items"]] == sorted(int(e["event_id"]) for e in events["items"])
    assert client.get(f"/api/operations/{operation['id']}/event-log?after={events['next_cursor']}").json()["items"] == []
    assert submit(task_context, session, key=key).json()["id"] == operation["id"]
    assert client.get(f"/api/training-targets/{task_context[1]['id']}").json()["version"] == 1
    row = query(pg_settings[1], "SELECT metadata FROM app.assessment_revisions WHERE session_id=%s", (session["id"],))[0]
    assert row["metadata"]["counts"]["provider_requests_admitted"] == 2 and row["metadata"]["counts"]["tool_executions"] == 4


def test_training_missing_executor_waits_until_explicit_resume(task_context, worker_settings, pg_settings):
    client = task_context[0]
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    message = message_for(pg_settings[1], operation)
    async def wait():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            await TaskWorker(repo, queue, "disabled").once()
            assert await repo.claim(message) is None
            assert await OutboxDispatcher(repo, queue).dispatch(0) == 0
    arun(wait())
    assert client.get(f"/api/operations/{operation['id']}").json()["status"] == "WAITING_CREDENTIALS"
    assert client.get(f"/api/practice-sessions/{session['id']}").json()["feedback"] is None
    assert client.post(f"/api/operations/{operation['id']}/resume", json={}).json()["status"] == "PENDING"
    async def finish():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch(0)
            await TaskWorker(repo, queue, "enabled", assess=scripted_assessment).once()
    arun(finish())
    assert client.get(f"/api/practice-sessions/{session['id']}").json()["status"] == "COMPLETED"


def test_training_cached_candidate_reused_after_lease_reclaim(task_context, worker_settings, pg_settings):
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    message = message_for(pg_settings[1], operation)
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            old = await repo.claim(message)
            context = await repo.assessment_context(old)
            output = await scripted_assessment(context, asyncio.Event())
            assert await repo.save_assessment(old, output)
            assert (await repo.saved_assessment(old)).candidate == output.candidate
            await expire(repo, message)
            assert not await repo.save_assessment(old, output)
            assert not await repo.complete(old)
            calls = []
            async def forbidden(*args):
                calls.append(True)
                raise AssertionError("cached candidate must not execute the graph again")
            await queue.publish(message)
            await TaskWorker(repo, queue, "cached", assess=forbidden).once()
            assert calls == []
            assert (await sql(repo, "SELECT attempt FROM app.background_jobs WHERE id=%s", (message.job_id,)))[0]["attempt"] == 2
    arun(scenario())
    assert task_context[0].get(f"/api/practice-sessions/{session['id']}").json()["status"] == "COMPLETED"


@pytest.mark.parametrize("corruption", ["hash", "quote"])
def test_training_corrupt_cache_has_no_final_feedback(task_context, worker_settings, pg_settings, corruption):
    session = practice(task_context).json()
    message = message_for(pg_settings[1], submit(task_context, session).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            output = await scripted_assessment(await repo.assessment_context(claim), asyncio.Event())
            assert await repo.save_assessment(claim, output)
            statement = ("UPDATE app.assessment_candidates SET input_hash=repeat('0',64) WHERE operation_id=%s RETURNING operation_id" if corruption == "hash" else
                         "UPDATE app.assessment_candidates SET candidate=jsonb_set(candidate,'{citations,0,quote}',to_jsonb('fabricated'::text)) WHERE operation_id=%s RETURNING operation_id")
            await sql(repo, statement, (message.operation_id,))
            await expire(repo, message)
            await queue.publish(message)
            await TaskWorker(repo, queue, "reject-cache", assess=scripted_assessment).once()
    arun(scenario())
    value = task_context[0].get(f"/api/practice-sessions/{session['id']}").json()
    assert value["status"] == "FAILED" and value["feedback"] is None
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.assessment_revisions WHERE session_id=%s", (session["id"],))[0]["n"] == 0


def test_training_invalid_runtime_citation_never_commits(task_context, worker_settings):
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    async def invalid(context, cancel):
        snapshot, run = build_snapshot(context)
        candidate = candidate_for(snapshot, run)
        candidate.citations[0].quote = "SYNTHETIC invented quote"
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(snapshot), ScriptedProvider(success_steps(snapshot, run, candidate))).run(run)
        assert result.envelope.candidate is None and result.envelope.termination != "candidate_ready"
        raise ValueError("scripted invalid candidate rejected")
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            await OutboxDispatcher(repo, queue).dispatch()
            await TaskWorker(repo, queue, "invalid-runtime", assess=invalid).once()
            assert await OutboxDispatcher(repo, queue).dispatch(0) == 0
    arun(scenario())
    value = task_context[0].get(f"/api/practice-sessions/{session['id']}").json()
    assert value["status"] == "FAILED" and value["feedback"] is None and value["answer"]["text"] == ANSWER


@pytest.mark.parametrize("after_candidate", [False, True])
def test_training_cancel_rejects_late_assessment(task_context, worker_settings, pg_settings, after_candidate):
    session = practice(task_context).json()
    operation = submit(task_context, session).json()
    message = message_for(pg_settings[1], operation)
    async def prepare():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            if after_candidate:
                assert await repo.save_assessment(claim, await scripted_assessment(await repo.assessment_context(claim), asyncio.Event()))
            return claim
    claim = arun(prepare())
    assert task_context[0].post(f"/api/operations/{operation['id']}/cancel", json={}).json()["status"] == "CANCELLED"
    async def verify():
        async with resources(worker_settings) as (repo, queue):
            assert not await repo.complete(claim) and not await repo.heartbeat(claim)
    arun(verify())
    value = task_context[0].get(f"/api/practice-sessions/{session['id']}").json()
    assert value["status"] == "CANCELLED" and value["feedback"] is None and value["answer"]["text"] == ANSWER


@pytest.mark.parametrize("change", ["target_version", "target_delete", "session_version", "session_delete"])
def test_training_stale_or_deleted_input_rejects_saved_candidate(task_context, worker_settings, pg_settings, change):
    client, target, _ = task_context
    session = practice(task_context).json()
    message = message_for(pg_settings[1], submit(task_context, session).json())
    async def prepare():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            assert await repo.save_assessment(claim, await scripted_assessment(await repo.assessment_context(claim), asyncio.Event()))
            return claim
    claim = arun(prepare())
    if change == "target_version":
        assert client.patch(f"/api/training-targets/{target['id']}", json={"expected_version": 1, "focus": "changed"}).status_code == 200
    elif change == "target_delete":
        assert client.request("DELETE", f"/api/training-targets/{target['id']}", json={"expected_version": 1}).status_code == 200
    elif change == "session_delete":
        assert client.request("DELETE", f"/api/practice-sessions/{session['id']}", json={"expected_version": 2}).status_code == 200
    else:
        query(pg_settings[1], "UPDATE app.practice_sessions SET version=version+1,status='CANCELLED' WHERE id=%s RETURNING id", (session["id"],))
    async def verify():
        async with resources(worker_settings) as (repo, queue):
            assert not await repo.complete(claim)
    arun(verify())
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.assessment_revisions WHERE session_id=%s", (session["id"],))[0]["n"] == 0
    if change.endswith("delete"):
        assert client.get(f"/api/practice-sessions/{session['id']}").status_code == 404
        assert client.get(f"/api/operations/{message.operation_id}").status_code == 404


def test_training_commit_revision_session_and_event_rollback_together(task_context, worker_settings, pg_settings, monkeypatch):
    session = practice(task_context).json()
    message = message_for(pg_settings[1], submit(task_context, session).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            assert await repo.save_assessment(claim, await scripted_assessment(await repo.assessment_context(claim), asyncio.Event()))
            original = repo._finish
            async def fail(*args, **kwargs):
                raise RuntimeError("SYNTHETIC failure after revision and session update")
            monkeypatch.setattr(repo, "_finish", fail)
            with pytest.raises(RuntimeError):
                await repo.complete(claim)
            assert (await sql(repo, "SELECT version,status FROM app.practice_sessions WHERE id=%s", (session["id"],)))[0] == {"version": 2, "status": "EVALUATING"}
            assert (await sql(repo, "SELECT count(*) AS n FROM app.assessment_revisions WHERE session_id=%s", (session["id"],)))[0]["n"] == 0
            assert [r["status"] for r in await sql(repo, "SELECT status FROM app.business_events WHERE operation_id=%s ORDER BY event_id", (message.operation_id,))] == ["PENDING", "RUNNING"]
            monkeypatch.setattr(repo, "_finish", original)
            assert await repo.complete(claim)
            assert not await repo.complete(claim)
    arun(scenario())


@pytest.mark.parametrize("method", ["complete", "heartbeat"])
def test_training_lease_checked_after_waiting_for_session_row(task_context, worker_settings, pg_settings, method):
    session = practice(task_context).json()
    message = message_for(pg_settings[1], submit(task_context, session).json())
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message, 0.3)
            assert await repo.save_assessment(claim, await scripted_assessment(await repo.assessment_context(claim), asyncio.Event()))
            async with await AsyncConnection.connect(pg_settings[1].conninfo(), autocommit=True) as blocker:
                async with blocker.transaction():
                    await blocker.execute("SELECT id FROM app.practice_sessions WHERE id=%s FOR UPDATE", (session["id"],))
                    waiting = asyncio.create_task(getattr(repo, method)(claim))
                    await asyncio.sleep(0.4)
                    assert not waiting.done()
                assert await waiting is False
    arun(scenario())


@pytest.mark.parametrize("cursor", ["-1", "x", "9223372036854775808"])
def test_training_event_cursor_validated(task_context, cursor):
    operation = submit(task_context, practice(task_context).json()).json()
    assert task_context[0].get(f"/api/operations/{operation['id']}/event-log?after={cursor}").status_code == 400
