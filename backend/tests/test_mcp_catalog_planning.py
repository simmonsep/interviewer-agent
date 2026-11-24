"""S04: the real worker path reaches the local MCP catalog over stdio (0 yuan, mock provider).

This complements `test_mcp_catalog.py`: there the tool seam is called directly; here the
`LivePlanner` runs inside the preparation worker, the model asks for the catalog through the
S03 tool, and the returned metadata is visible in the next (mocked) model request.
"""
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from interview_agent.billing.ledger import authorize
from interview_agent.interview_prep import checkpointer
from interview_agent.interview_prep.planner import LivePlanner
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.interview_prep.worker import worker_for
from interview_agent.interviews.billing import InterviewPolicy
from interview_agent.worker.queue import OutboxDispatcher
from test_interview_prep_repository import checkpoints, context, create, view  # noqa: F401 (fixtures)
from test_postgres import arun, pg_settings, query  # noqa: F401 (fixtures)
from test_worker import resources, worker_settings  # noqa: F401 (fixtures)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

USAGE = {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60,
         "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 40}


def completion(policy, message, finish):
    return {"id": "offline-mcp", "object": "chat.completion", "created": 1,
            "model": policy.model,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": USAGE}


def test_a_preparation_reads_the_catalog_through_mcp_and_stays_zero_cost(context, worker_settings,
                                                                          pg_settings, tmp_path):
    client, goal, _uid = context
    policy = InterviewPolicy.load(Path("configs/ja05-deepseek-flash.json"))
    key = tmp_path / "key.env"
    key.write_text("DEEPSEEK_API_KEY=offline-not-real\n", encoding="utf-8")
    identity = f"test-mcp-{uuid4().hex}"
    arun(authorize(pg_settings[1], policy, identity, 200_000_000))
    prep = create(client, goal).json()
    bodies = []
    call = {"id": "call-mcp", "type": "function",
            "function": {"name": "list_builtin_interview_topics", "arguments": "{}"}}

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            message = {"role": "assistant", "content": "", "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": json.dumps(
                {"focus_topic_ids": ["java-concurrency"], "resume_item_indexes": [],
                 "evidence_ids": [], "rationale": "先补并发。"}, ensure_ascii=False)}
            finish = "stop"
        return httpx.Response(200, json=completion(policy, message, finish))

    async def scenario():
        async with resources(worker_settings) as (old, queue):
            repo = PreparationRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            worker = worker_for(
                repo, queue, pool=old.pool,
                saver_factory=lambda: checkpointer.saver(worker_settings.database),
                planner_factory=lambda claim: LivePlanner(
                    old.pool, repo, claim, policy, identity, key,
                    inner_factory=lambda: httpx.MockTransport(handler)))
            return await worker.once()

    try:
        assert arun(scenario())
        row = view(client, goal, prep["id"])
        assert row["status"] == "WAITING_FOR_USER", row.get("error_code")
        assert row["plan"]["planner"] == "langchain-agent"
        assert row["plan"]["focus_topic_ids"] == ["java-concurrency"]
        assert len(bodies) == 2  # one tool round, one answer round
        # The catalog metadata the model saw came from the MCP child process.
        second_request = json.dumps(bodies[1], ensure_ascii=False)
        assert "ja05-role-pack-v1" in second_request
        assert "redis-lock" in second_request
        assert '"base"' not in second_request and '"probe"' not in second_request
        calls = query(pg_settings[1], "SELECT state FROM app.model_http_calls "
                                      "WHERE authorization_id=%s ORDER BY created_at", (identity,))
        assert [row["state"] for row in calls] == ["KNOWN", "KNOWN"]  # mock usage, settled
    finally:
        query(pg_settings[1],
              "DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id", (identity,))
        query(pg_settings[1],
              "DELETE FROM app.model_authorizations WHERE id=%s RETURNING id", (identity,))


def test_a_broken_catalog_fails_before_the_first_paid_request(context, worker_settings,
                                                              pg_settings, tmp_path, monkeypatch):
    """S04c: a server that starts but disagrees must fail at 0 model calls.

    Before the preflight this cost one or more real requests and only failed after the turn,
    which is money spent to learn that the catalog was wrong all along.
    """
    import sys

    from mcp import StdioServerParameters

    from interview_agent.interview_prep import mcp_client

    client, goal, _uid = context
    policy = InterviewPolicy.load(Path("configs/ja05-deepseek-flash.json"))
    key = tmp_path / "key.env"
    key.write_text("DEEPSEEK_API_KEY=offline-not-real\n", encoding="utf-8")
    identity = f"test-mcp-bad-{uuid4().hex}"
    arun(authorize(pg_settings[1], policy, identity, 200_000_000))
    prep = create(client, goal).json()
    bodies = []

    def handler(request):  # would be a paid call; it must never run
        bodies.append(request)
        return httpx.Response(200, json=completion(
            policy, {"role": "assistant", "content": "{}"}, "stop"))

    fixtures = Path(__file__).parent / "mcp_fixtures"

    def wrong_server():
        return StdioServerParameters(command=sys.executable,
                                     args=[str(fixtures / "wrong_catalog.py")],
                                     env={"PYTHONPATH": str(mcp_client.SOURCE_ROOT)})

    monkeypatch.setattr(mcp_client, "default_server_params", wrong_server)

    async def scenario():
        async with resources(worker_settings) as (old, queue):
            repo = PreparationRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            worker = worker_for(
                repo, queue, pool=old.pool,
                saver_factory=lambda: checkpointer.saver(worker_settings.database),
                planner_factory=lambda claim: LivePlanner(
                    old.pool, repo, claim, policy, identity, key,
                    inner_factory=lambda: httpx.MockTransport(handler)))
            return await worker.once()

    try:
        assert arun(scenario())
        row = view(client, goal, prep["id"])
        assert row["status"] == "FAILED"
        assert row["error_code"] == "mcp_catalog_unavailable"
        detail = query(pg_settings[1],
                       "SELECT error_detail FROM app.career_interview_preparations WHERE id=%s",
                       (row["id"],))[0]["error_detail"]
        assert detail["mcp_code"] == "mcp_catalog_mismatch"
        assert bodies == []  # nothing left the process
        assert query(pg_settings[1], "SELECT id FROM app.model_http_calls WHERE authorization_id=%s",
                     (identity,)) == []
    finally:
        query(pg_settings[1],
              "DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id", (identity,))
        query(pg_settings[1],
              "DELETE FROM app.model_authorizations WHERE id=%s RETURNING id", (identity,))
