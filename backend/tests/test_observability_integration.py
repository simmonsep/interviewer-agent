"""S00O: the real API/Worker/queue chain, queried by session and run id."""
import asyncio
from uuid import uuid4

import pytest

from interview_agent import observability as obs
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.interviews.repository import InterviewRepository
from interview_agent.interviews.worker import worker_for
from interview_agent.worker.queue import OutboxDispatcher, TaskWorker
from test_postgres import arun, client_for, pg_settings, query, signup
from test_worker import resources, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

GOAL = {"message": "准备Java后端面试，无简历也可以。", "conditions": {"direction": "Java后端"}}


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """Enable tracing exactly as an operator would: two environment variables."""
    monkeypatch.setenv("INTERVIEW_AGENTOPS", "1")
    monkeypatch.setenv("INTERVIEW_AGENTOPS_DIR", str(tmp_path))
    try:
        yield tmp_path
    finally:
        obs.reset()


def post(client, path, body, key=None):
    return client.post(path, json=body, headers={"Idempotency-Key": key or uuid4().hex})


def goal(client):
    return post(client, "/api/career-goals/", GOAL).json()


def create(client, g):
    return post(client, f"/api/career-goals/{g['id']}/interviews",
                {"expected_version": 1, "role": "java-backend", "max_questions": 2}).json()


def view(client, g, s):
    return client.get(f"/api/career-goals/{g['id']}/interviews/{s['id']}").json()


def answer(client, g, s, text):
    question = next(e for e in reversed(s["events"]) if e["kind"] == "question")
    return post(client, f"/api/career-goals/{g['id']}/interviews/{s['id']}/answers",
                {"expected_version": s["version"], "question_id": question["id"], "text": text})


async def once(settings, tracer=None):
    async with resources(settings) as (old, q):
        repo = InterviewRepository(old.pool)
        await OutboxDispatcher(repo, q).dispatch()
        assert await worker_for(repo, q, provider="scripted", tracer=tracer).once()


def rows_for(directory, **kwargs):
    all_rows, unreadable = obs.load(directory)
    assert unreadable == 0
    return all_rows, obs.select(all_rows, **kwargs)


def test_two_question_session_is_queryable_by_session_and_run(traced, pg_settings, worker_settings):
    with client_for(pg_settings[0]) as c:
        c.headers["Origin"] = pg_settings[0].origin
        first = signup(c)["user"]["id"]
        second = signup(c)["user"]["id"]
        try:
            g = goal(c)
            s = create(c, g)
            arun(once(worker_settings))
            s = view(c, g, s)
            for text in ("不知道，只加volatile。", "我会把检查与更新做成一个原子操作，并说明验证边界。"):
                assert answer(c, g, s, text).status_code == 202
                arun(once(worker_settings))
                s = view(c, g, s)
            assert s["status"] == "COMPLETED" and s["report"]["questions"] == 2

            # A second account's session exists in the same file but must never be selected.
            other_goal = goal(c)
            other = create(c, other_goal)

            tracer = obs.current()
            assert tracer.enabled is True
            tracer.flush()
            all_rows, chosen = rows_for(traced, session_id=s["id"])
            names = [row["event_name"] for row in chosen]
            assert names.count("interview.created") == 1
            assert names.count("answer.accepted") == 2
            assert names.count("worker.attempt") == 3
            assert names.count("interview.decision") == 3
            assert names.count("interview.model_round") == 9
            assert names.count("interview.tool") == 9
            # Scripted never opens a provider socket, so there is no HTTP span and no money.
            assert not [row for row in chosen if row["event_name"] == "provider.http"]
            assert {row["session_id"] for row in chosen} == {s["id"]}
            assert {row["goal_id"] for row in chosen if row.get("goal_id")} == {g["id"]}

            decision_runs = {row["interview_run_id"] for row in chosen if row["event_name"] == "interview.decision"}
            assert len(decision_runs) == 3 and s["run"]["id"] in decision_runs
            # One run id selects one attempt, the answer that produced it, and the Redis
            # notification that started it (the notification only has the operation id).
            one_run = obs.select(all_rows, run_id=s["run"]["id"])
            for row in one_run:
                assert s["run"]["id"] in {row.get("interview_run_id"), row.get("operation_id")}
            run_names = [row["event_name"] for row in one_run]
            assert run_names.count("queue.notification") == 1
            assert run_names.count("answer.accepted") == 1
            assert run_names.count("worker.attempt") == 1
            assert run_names.count("interview.decision") == 1

            _, other_rows = rows_for(traced, session_id=other["id"])
            assert {row["session_id"] for row in other_rows} == {other["id"]}
            assert not ({row["span_id"] for row in other_rows} & {row["span_id"] for row in chosen})

            summary = obs.summarize(chosen)
            assert summary["failures_by_category"] == {}
            assert summary["model_http_calls"] == 0
            assert summary["stages"] == {"api": 3, "worker": 9, "interview": 18, "queue": 3}
            # The Redis payload stays exactly three fields, so "notification received" can only
            # be keyed by operation id. Pivot: session -> interview.created/interview.decision
            # gives the run id; that run id selects the notification that started it.
            notification = [row for row in all_rows if row["event_name"] == "queue.notification"]
            assert notification and all(row.get("session_id") is None for row in notification)
            assert run_names.count("queue.notification") == 1
        finally:
            query(pg_settings[1], "DELETE FROM auth.users WHERE id=ANY(%s::uuid[]) RETURNING id", ([first, second],))


def test_admission_refusal_is_traced_without_any_provider_call(traced, pg_settings, worker_settings, monkeypatch):
    async def refuse(self, claim):
        raise BillingUnavailable("model_budget_exhausted", "training_model_requests")

    monkeypatch.setattr(InterviewRepository, "admit_scripted", refuse)
    with client_for(pg_settings[0]) as c:
        c.headers["Origin"] = pg_settings[0].origin
        uid = signup(c)["user"]["id"]
        try:
            g = goal(c)
            s = create(c, g)
            arun(once(worker_settings))
            s = view(c, g, s)
            assert s["status"] == "WAITING_CREDENTIALS" and s["run"]["error_code"] == "model_budget_exhausted"
            obs.current().flush()
            _, chosen = rows_for(traced, session_id=s["id"])
            failed = [row for row in chosen if row["event_name"] == "interview.failed"]
            assert len(failed) == 1
            assert failed[0]["reason_code"] == "model_budget_exhausted"
            assert failed[0]["error_category"] == "budget"
            assert failed[0]["outcome"] == "WAITING_CREDENTIALS"
            names = [row["event_name"] for row in chosen]
            assert "provider.http" not in names and "interview.decision" not in names
            attempt = [row for row in chosen if row["event_name"] == "worker.attempt"][0]
            assert attempt["status"] == "unset" and attempt["attempt"] == 1
        finally:
            query(pg_settings[1], "DELETE FROM auth.users WHERE id=%s RETURNING id", (uid,))


def test_lost_lease_is_traced_and_the_result_is_not_committed(traced, pg_settings, worker_settings, monkeypatch):
    async def never(self, claim, lease_seconds=30):
        return False

    monkeypatch.setattr(InterviewRepository, "heartbeat", never)

    async def slow_prepare(claim, lost):
        await asyncio.sleep(1.2)

    with client_for(pg_settings[0]) as c:
        c.headers["Origin"] = pg_settings[0].origin
        uid = signup(c)["user"]["id"]
        try:
            g = goal(c)
            s = create(c, g)

            async def scenario():
                async with resources(worker_settings) as (old, q):
                    repo = InterviewRepository(old.pool)
                    await OutboxDispatcher(repo, q).dispatch()
                    worker = TaskWorker(repo, q, "s00o-" + uuid4().hex, lease_seconds=1, prepare=slow_prepare)
                    assert await worker.once()

            arun(scenario())
            s = view(c, g, s)
            assert s["status"] == "RUNNING" and s["run"]["status"] == "RUNNING"
            obs.current().flush()
            _, chosen = rows_for(traced, session_id=s["id"])
            names = [row["event_name"] for row in chosen]
            assert names.count("worker.lease_lost") == 1
            lost = [row for row in chosen if row["event_name"] == "worker.lease_lost"][0]
            assert lost["reason_code"] == "heartbeat_rejected" and lost["error_category"] == "queue"
            assert "worker.result_committed" not in names and "interview.decision" not in names
            assert "worker.acked" not in names
        finally:
            query(pg_settings[1], "DELETE FROM auth.users WHERE id=%s RETURNING id", (uid,))
