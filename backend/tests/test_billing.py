"""Real SQL/SDK/Graph with HTTPX MockTransport. No paid network requests or real credentials."""

import asyncio
import json
import gzip
import zstandard
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

from interview_agent.billing.config import LivePolicy
from interview_agent.billing.ledger import BillingLedger, BillingUnavailable, authorize, usage_upper, resume_held
from interview_agent.domain.practice import build_snapshot
from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
from interview_agent.runtime.scripted import success_steps
from interview_agent.runtime.tools import specifications
from interview_agent.runtime.types import Message
from interview_agent.worker.assessment import AssessmentUnavailable
from interview_agent.worker.live import live_assessment
from interview_agent.worker.queue import OutboxDispatcher, TaskWorker
from test_postgres import arun, pg_settings, query
from test_training import practice, submit
from test_worker import expire, message_for, resources, sql, task_context, worker_settings

pytestmark = [pytest.mark.postgres, pytest.mark.redis]
USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60}


@pytest.fixture
def billing_auth(pg_settings, project_root, tmp_path):
    policy = LivePolicy.load(project_root / "configs/py008-deepseek-flash.json")
    key = tmp_path / "credentials.env"
    key.write_text("DEEPSEEK_API_KEY=offline-test-not-a-real-key\n", encoding="utf-8")
    ids = []
    def create(*, limit=2_000_000_000, overrides=None):
        selected = LivePolicy.model_validate({**policy.model_dump(), **(overrides or {})})
        identity = f"test-{uuid4().hex}"
        arun(authorize(pg_settings[1], selected, identity, limit))
        ids.append(identity)
        return selected, identity, key
    yield create
    for identity in ids:
        query(pg_settings[1], "DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id", (identity,))
        query(pg_settings[1], "DELETE FROM app.model_authorizations WHERE id=%s RETURNING id", (identity,))


def reply(response, *, usage=USAGE):
    calls = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}} for c in response.tool_calls]
    return {"id": f"offline-{uuid4().hex}", "object": "chat.completion", "created": 1,
        "model": "DeepSeek-V4.1-Flash-1017", "choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": response.text, "tool_calls": calls}}], "usage": usage}


def prepare(context, owner):
    session = practice(context).json()
    operation = submit(context, session).json()
    return session, operation, message_for(owner, operation)


def test_billing_real_sdk_graph_wire_usage_and_sql_commit(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    session, operation, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            snapshot, run = build_snapshot(context)
            steps, sent = success_steps(snapshot, run), []
            ledger = BillingLedger(repo.pool, policy, identity)
            async def respond(request):
                audit = await ledger.summary(context.operation_id)
                assert audit["http_attempts"] == len(sent) + 1 and audit["calls"][-1]["state"] == "RESERVED"
                payload = json.loads(request.content)
                assert payload["thinking"] == {"type": "disabled"} and payload["stream"] is False
                assert payload["max_tokens"] == 2048 and payload["model"] == "deepseek-flash"
                assert str(request.url) == policy.endpoint and {t["function"]["name"] for t in payload["tools"]} == set(run.allowed_tools)
                sent.append(payload)
                return httpx.Response(200, json=reply(steps[len(sent)-1]))
            output = await live_assessment(repo, claim, context, asyncio.Event(), policy=policy,
                authorization_id=identity, credentials=key, inner=httpx.MockTransport(respond))
            assert len(sent) == output.metadata.http_attempts == 2
            assert output.metadata.origin == "real_model"  # Offline HTTP fixture, not a claim of actual model quality.
            assert any(m["role"] == "tool" and "我的原回答" in m["content"] for m in sent[1]["messages"])
            audit = await ledger.summary(operation["id"])
            assert audit["usage_status"] == "known" and audit["calls"][0]["raw_usage"] == USAGE
            assert audit["accounted_upper_nano"] == 2 * usage_upper(policy, USAGE)[1]
            assert await repo.save_assessment(claim, output) and await repo.complete(claim)
    arun(scenario())
    value = task_context[0].get(f"/api/practice-sessions/{session['id']}").json()
    assert value["status"] == "COMPLETED" and value["feedback"]["provider"] == "deepseek-flash"


@pytest.mark.parametrize("status", [429, 500, "timeout"])
def test_billing_sdk_has_zero_http_retries_and_unknown_stays_held(task_context, worker_settings, pg_settings, billing_auth, status):
    policy, identity, key = billing_auth()
    _, operation, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            sent = []
            def respond(request):
                sent.append(1)
                if status == "timeout":
                    raise httpx.ReadTimeout("SYNTHETIC response timeout", request=request)
                return httpx.Response(status, json={"error": {"message": "SYNTHETIC HTTP failure"}})
            for _ in range(2):
                with pytest.raises(AssessmentUnavailable, match="model_usage_unknown"):
                    await live_assessment(repo, claim, context, asyncio.Event(), policy=policy,
                        authorization_id=identity, credentials=key, inner=httpx.MockTransport(respond))
            assert len(sent) == 1
            audit = await BillingLedger(repo.pool, policy, identity).summary()
            assert audit["usage_status"] == "unknown" and audit["accounted_upper_nano"] == policy.reservation_nano
            assert audit["calls"][0]["accounted_nano"] is None
    arun(scenario())


def test_billing_partial_usage_is_preserved_and_not_zero(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            steps = success_steps(*build_snapshot(context))
            with pytest.raises(AssessmentUnavailable, match="model_usage_unknown"):
                await live_assessment(repo, claim, context, asyncio.Event(), policy=policy, authorization_id=identity,
                    credentials=key, inner=httpx.MockTransport(lambda _: httpx.Response(200, json=reply(steps[0], usage={"prompt_tokens": 100}))))
            row = (await BillingLedger(repo.pool, policy, identity).summary())["calls"][0]
            assert row["raw_usage"] == {"prompt_tokens": 100} and row["accounted_nano"] is None and row["state"] == "UNKNOWN"
    arun(scenario())


def test_billing_budget_refusal_sends_zero_requests_and_waits(task_context, worker_settings, pg_settings, billing_auth):
    base = LivePolicy.load(Path(__file__).resolve().parents[2] / "configs/py008-deepseek-flash.json")
    policy, identity, key = billing_auth(limit=base.reservation_nano - 1)
    session, operation, _ = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            sent = []
            async def assess(claim, context, cancel):
                return await live_assessment(repo, claim, context, cancel, policy=policy, authorization_id=identity,
                    credentials=key, inner=httpx.MockTransport(lambda _: sent.append(1)))
            await OutboxDispatcher(repo, queue).dispatch()
            assert await TaskWorker(repo, queue, "no-budget", claimed_assess=assess).once()
            assert not sent and (await BillingLedger(repo.pool, policy, identity).summary())["http_attempts"] == 0
    arun(scenario())
    saved = task_context[0].get(f"/api/practice-sessions/{session['id']}").json()
    assert saved["operation"]["status"] == "WAITING_CREDENTIALS" and saved["operation"]["error_code"] == "model_budget_exhausted"
    assert saved["answer"] and saved["feedback"] is None


def test_billing_missing_key_waits_before_http(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    key.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            claim = await repo.claim(message)
            with pytest.raises(AssessmentUnavailable, match="model_credentials_unavailable"):
                await live_assessment(repo, claim, await repo.assessment_context(claim), asyncio.Event(),
                    policy=policy, authorization_id=identity, credentials=key)
            assert (await BillingLedger(repo.pool, policy, identity).summary())["http_attempts"] == 0
    arun(scenario())


def test_billing_payload_limit_blocks_sdk_before_reservation(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            from interview_agent.billing.config import load_key
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            sent = []
            ledger = BillingLedger(repo.pool, policy, identity)
            transport = BudgetedTransport(httpx.MockTransport(lambda _: sent.append(1)), ledger, claim, context)
            provider = DeepSeekProvider(load_key(key), transport)
            try:
                with pytest.raises(Exception):
                    await provider.complete([Message(role="user", text="x" * 40000)], specifications(list(build_snapshot(context)[1].allowed_tools)))
                assert transport.failure_code == "model_payload_rejected" and not sent
                assert (await ledger.summary())["http_attempts"] == 0
            finally:
                await provider.aclose()
    arun(scenario())


def test_billing_concurrent_global_reservations_do_not_overrun(task_context, worker_settings, pg_settings, billing_auth):
    base = LivePolicy.load(Path(__file__).resolve().parents[2] / "configs/py008-deepseek-flash.json")
    policy, identity, _ = billing_auth(limit=base.reservation_nano * 2)
    messages = [prepare(task_context, pg_settings[1])[2] for _ in range(4)]
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            claims = [await repo.claim(message) for message in messages]
            contexts = [await repo.assessment_context(claim) for claim in claims]
            ledger = BillingLedger(repo.pool, policy, identity)
            results = await asyncio.gather(*(ledger.reserve(claim, context, "a" * 64, 100)
                for claim, context in zip(claims, contexts)), return_exceptions=True)
            assert sum(isinstance(r, str) for r in results) == 2
            assert sum(isinstance(r, BillingUnavailable) for r in results) == 2
            assert (await ledger.summary())["accounted_upper_nano"] == 2 * policy.reservation_nano
    arun(scenario())


def test_billing_unsettled_call_survives_reclaim_and_blocks_paid_replay(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            old = await repo.claim(message)
            ledger = BillingLedger(repo.pool, policy, identity)
            await ledger.reserve(old, await repo.assessment_context(old), "a" * 64, 100)
            await expire(repo, message)
            new = await repo.claim(message)
            sent = []
            with pytest.raises(AssessmentUnavailable, match="model_usage_unknown"):
                await live_assessment(repo, new, await repo.assessment_context(new), asyncio.Event(), policy=policy,
                    authorization_id=identity, credentials=key, inner=httpx.MockTransport(lambda _: sent.append(1)))
            row = (await BillingLedger(repo.pool, policy, identity).summary())["calls"][0]
            assert not sent and row["state"] == "RESERVED" and row["accounted_nano"] is None
    arun(scenario())


def test_billing_operation_budget_persists_across_worker_attempts(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, _ = billing_auth(overrides={"max_operation_attempts": 2})
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            ledger = BillingLedger(repo.pool, policy, identity)
            claim = await repo.claim(message)
            first = await ledger.reserve(claim, await repo.assessment_context(claim), "a" * 64, 100)
            assert await ledger.settle(first, http_status=200, usage=USAGE)
            await ledger.settle(first, http_status=200, usage={**USAGE, "prompt_tokens": 1})
            assert (await ledger.summary())["accounted_upper_nano"] == usage_upper(policy, USAGE)[1]
            await expire(repo, message)
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            second = await ledger.reserve(claim, context, "b" * 64, 100)
            await ledger.settle(second, http_status=200, usage=USAGE)
            with pytest.raises(BillingUnavailable, match="model_budget_exhausted"):
                await ledger.reserve(claim, context, "c" * 64, 100)
    arun(scenario())


def test_billing_stale_claim_cannot_send_or_reserve(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, _ = billing_auth()
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            old = await repo.claim(message)
            context = await repo.assessment_context(old)
            await expire(repo, message)
            await repo.claim(message)
            ledger = BillingLedger(repo.pool, policy, identity)
            with pytest.raises(BillingUnavailable, match="model_execution_expired"):
                await ledger.reserve(old, context, "a" * 64, 100)
            assert not (await ledger.summary())["calls"]
    arun(scenario())


def test_billing_cancellation_records_unknown_and_no_retry(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            started = asyncio.Event()
            sent = []
            async def respond(_):
                sent.append(1);started.set()
                await asyncio.Event().wait()
            task = asyncio.create_task(live_assessment(repo, claim, context, asyncio.Event(), policy=policy,
                authorization_id=identity, credentials=key, inner=httpx.MockTransport(respond)))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            summary = await BillingLedger(repo.pool, policy, identity).summary()
            assert len(sent) == 1 and summary["usage_status"] == "unknown" and summary["accounted_upper_nano"] == policy.reservation_nano
    arun(scenario())


def test_billing_app_cannot_raise_authorization_or_delete_accounting(pg_settings, worker_settings, billing_auth):
    _, identity, _ = billing_auth()
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            for statement in ("UPDATE app.model_authorizations SET max_nano=2000000000 WHERE id=%s RETURNING id",
                              "DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await sql(repo, statement, (identity,))
    arun(scenario())


def test_billing_invalid_model_tool_json_is_charged_known_but_not_committed(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    session, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            raw = reply(success_steps(*build_snapshot(context))[0])
            raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "NOT JSON"
            with pytest.raises(ValueError, match="live_assessment_candidate_not_ready"):
                await live_assessment(repo, claim, context, asyncio.Event(), policy=policy, authorization_id=identity,
                    credentials=key, inner=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)))
            audit = await BillingLedger(repo.pool, policy, identity).summary()
            assert audit["http_attempts"] == 1 and audit["usage_status"] == "known"
    arun(scenario())
    assert query(pg_settings[1], "SELECT count(*) AS n FROM app.assessment_revisions WHERE session_id=%s", (session["id"],))[0]["n"] == 0


def test_billing_real_candidate_cache_reclaim_sends_no_new_http(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, key = billing_auth()
    session, operation, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, queue):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            steps, sent = success_steps(*build_snapshot(context)), []
            def respond(_):
                sent.append(1)
                return httpx.Response(200, json=reply(steps[len(sent)-1]))
            output = await live_assessment(repo, claim, context, asyncio.Event(), policy=policy,
                authorization_id=identity, credentials=key, inner=httpx.MockTransport(respond))
            assert await repo.save_assessment(claim, output)
            await expire(repo, message)
            async def forbidden(*args):
                raise AssertionError("cached candidate must not make another model request")
            await queue.publish(message)
            assert await TaskWorker(repo, queue, "cached-live", claimed_assess=forbidden).once()
            assert len(sent) == 2 and (await BillingLedger(repo.pool, policy, identity).summary())["http_attempts"] == 2
    arun(scenario())
    assert task_context[0].get(f"/api/practice-sessions/{session['id']}").json()["status"] == "COMPLETED"


@pytest.mark.parametrize("encoding", ["gzip", "zstd"])
def test_billing_compressed_http_response_usage_is_decoded_before_sdk(task_context, worker_settings, pg_settings, billing_auth, encoding):
    policy, identity, key = billing_auth()
    _, _, message = prepare(task_context, pg_settings[1])
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            claim = await repo.claim(message)
            context = await repo.assessment_context(claim)
            steps, sent = success_steps(*build_snapshot(context)), []
            def respond(_):
                sent.append(1)
                content = json.dumps(reply(steps[len(sent)-1])).encode()
                body = gzip.compress(content) if encoding == "gzip" else zstandard.ZstdCompressor().compress(content)
                return httpx.Response(200, stream=httpx.ByteStream(body), headers={
                    "Content-Type": "application/json", "Content-Encoding": encoding, "Content-Length": str(len(body))})
            output = await live_assessment(repo, claim, context, asyncio.Event(), policy=policy, authorization_id=identity,
                credentials=key, inner=httpx.MockTransport(respond))
            assert output.metadata.http_attempts == 2 and len(sent) == 2
            assert (await BillingLedger(repo.pool, policy, identity).summary())["usage_status"] == "known"
            assert (await BillingLedger(repo.pool, policy, identity).summary())["calls"][0]["response_meta"]["content_encoding"] == encoding
    arun(scenario())


def test_billing_explicit_resume_keeps_uncertain_charge_and_never_replays_old_operation(task_context, worker_settings, pg_settings, billing_auth):
    policy, identity, _ = billing_auth()
    first, second = [prepare(task_context, pg_settings[1])[2] for _ in range(2)]
    async def scenario():
        async with resources(worker_settings) as (repo, _):
            ledger = BillingLedger(repo.pool, policy, identity)
            old = await repo.claim(first)
            context = await repo.assessment_context(old)
            call_id = await ledger.reserve(old, context, "a" * 64, 100)
            await ledger.settle(call_id, http_status=200)
            assert (await ledger.summary())["authorization_state"] == "PAUSED"
            await resume_held(pg_settings[1], policy, identity)
            audit = await ledger.summary()
            assert audit["authorization_state"] == "ACTIVE" and audit["accounted_upper_nano"] == policy.reservation_nano
            assert audit["http_attempts"] == 1 and audit["calls"][0]["state"] == "UNKNOWN"
            with pytest.raises(BillingUnavailable, match="model_usage_unknown"):
                await ledger.reserve(old, context, "b" * 64, 100)
            new = await repo.claim(second)
            await ledger.reserve(new, await repo.assessment_context(new), "c" * 64, 100)
            assert (await ledger.summary())["accounted_upper_nano"] == 2 * policy.reservation_nano
    arun(scenario())
