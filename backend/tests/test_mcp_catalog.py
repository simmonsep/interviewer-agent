"""S04: the local stdio MCP catalog server and its client, including failure paths.

The happy path is exercised twice with different code: once with the raw official SDK
(`initialize` → `list_tools` → `call_tool`, no project adapter) and once through the S03
planning tool seam. Every other test pins one stable failure code.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from interview_agent.interview_prep import mcp_catalog
from interview_agent.interview_prep.mcp_client import (DEFAULT_TIMEOUT_SECONDS, GET_TOOL,
                                                       LIST_TOOL, SOURCE_ROOT, McpCatalogClient,
                                                       McpCatalogError, default_server_params)
from interview_agent.interview_prep.planning import _tools, build_planner
from interview_agent.interviews.catalog import pack
from test_interview_planning import RecordingModel, scope

FIXTURES = Path(__file__).parent / "mcp_fixtures"
ROLE = "java-backend"


def run(awaitable):
    return asyncio.run(awaitable)


def fixture_params(script, env=None):
    return StdioServerParameters(command=sys.executable, args=[str(FIXTURES / script)],
                                 env={"PYTHONPATH": str(SOURCE_ROOT), **(env or {})})


def test_the_server_projection_is_metadata_only():
    payload = mcp_catalog.catalog_payload(ROLE)
    expected = pack(ROLE)
    assert payload["catalog_version"] == expected["version"] == "ja05-role-pack-v1"
    assert payload["sha256"] == expected["sha256"]
    assert [topic["id"] for topic in payload["topics"]] == expected["topic_order"]
    for topic in payload["topics"]:
        assert set(topic) == {"id", "title", "url"}  # no base/probe/retest/reference
        assert topic["url"].startswith("https://")


def test_the_raw_sdk_discovers_then_calls_the_server():
    """Independent verification: no project adapter on the path."""
    async def scenario():
        async with stdio_client(default_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {LIST_TOOL, GET_TOOL}
                result = await session.call_tool(LIST_TOOL, {"role": ROLE})
                assert result.isError is False
                payload = json.loads(result.content[0].text)
                assert payload["catalog_version"] == pack(ROLE)["version"]
                assert [topic["id"] for topic in payload["topics"]] == pack(ROLE)["topic_order"]
                detail = await session.call_tool(GET_TOOL, {"role": ROLE, "topic_id": "redis-lock"})
                assert json.loads(detail.content[0].text)["topic"]["id"] == "redis-lock"

    run(scenario())


def test_invalid_role_and_unknown_topic_are_server_side_errors():
    """The role check lives in the server too; a hand-written client cannot bypass it."""
    async def scenario():
        async with stdio_client(default_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                bad_role = await session.call_tool(LIST_TOOL, {"role": "not-a-role"})
                missing = await session.call_tool(LIST_TOOL, {})
                unknown = await session.call_tool(GET_TOOL, {"role": ROLE, "topic_id": "nope"})
                return bad_role, missing, unknown

    bad_role, missing, unknown = run(scenario())
    assert bad_role.isError is True and missing.isError is True and unknown.isError is True


def test_the_client_lists_and_gets_the_same_pack_through_stdio():
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20) as catalog:
            listed = await catalog.list_topics()
            detail = await catalog.get_topic("sql-isolation")
            return listed, detail

    listed, detail = run(scenario())
    assert listed["catalog_version"] == pack(ROLE)["version"]
    assert [topic["id"] for topic in listed["topics"]] == pack(ROLE)["topic_order"]
    assert detail["topic"]["title"] == pack(ROLE)["topics"]["sql-isolation"]["title"]
    assert "reference" not in detail["topic"] and "base" not in detail["topic"]


def test_an_unknown_topic_is_refused_by_the_client():
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20) as catalog:
            await catalog.get_topic("not-a-topic")

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code == "mcp_tool_error"


def test_an_unknown_role_is_refused_before_a_process_is_started():
    with pytest.raises(McpCatalogError) as error:
        McpCatalogClient("not-a-role")
    assert error.value.code == "mcp_role_not_in_catalog"


def test_an_extra_declared_tool_is_a_schema_mismatch():
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20,
                                    server_params=fixture_params("extra_tool.py")) as catalog:
            await catalog.list_topics()

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code == "mcp_tool_schema_mismatch"


def test_a_catalog_from_another_version_is_a_mismatch():
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20,
                                    server_params=fixture_params("wrong_catalog.py")) as catalog:
            await catalog.list_topics()

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code == "mcp_catalog_mismatch"
    assert error.value.detail == {"field": "catalog_version"}


def test_a_non_catalog_payload_is_a_bad_response():
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20,
                                    server_params=fixture_params("bad_payload.py")) as catalog:
            await catalog.list_topics()

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code == "mcp_bad_response"


def test_an_unparseable_stream_is_a_bounded_failure():
    """The SDK turns a non-JSON line into a read-stream error; the call must still be bounded."""
    started = time.monotonic()
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=2,
                                    server_params=fixture_params("garbage.py")) as catalog:
            await catalog.list_topics()

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code == "mcp_timeout"
    assert time.monotonic() - started < 15


def test_a_hanging_server_times_out():
    started = time.monotonic()
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=0.6,
                                    server_params=fixture_params("hang.py")) as catalog:
            await catalog.list_topics()

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code == "mcp_timeout"
    assert time.monotonic() - started < DEFAULT_TIMEOUT_SECONDS


def test_a_server_that_exits_is_reported_without_retry():
    params = StdioServerParameters(command=sys.executable,
                                   args=["-c", "import sys; sys.exit(3)"], env={})
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=5, server_params=params) as catalog:
            await catalog.list_topics()

    with pytest.raises(McpCatalogError) as error:
        run(scenario())
    assert error.value.code in {"mcp_unavailable", "mcp_protocol_error"}


def test_closing_the_client_shuts_the_server_down(tmp_path):
    """stdin closes → the server's lifespan runs → its own shutdown line is written."""
    marker = tmp_path / "lifecycle.txt"
    params = default_server_params()
    params.env = {**(params.env or {}), "INTERVIEW_MCP_LIFECYCLE_FILE": str(marker)}

    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20, server_params=params) as catalog:
            await catalog.list_topics()
        with pytest.raises(McpCatalogError):
            await catalog.list_topics()  # a closed client cannot be reused

    run(scenario())
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0].split()[0] == "started" and lines[-1].split()[0] == "stopped"
    assert len({line.split()[1] for line in lines}) == 1  # same process start and stop


def test_the_planning_tool_goes_through_the_catalog():
    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20) as catalog:
            tools = {tool.__name__: tool for tool in _tools(scope(), catalog, [])}
            return await tools[LIST_TOOL]()

    payload = json.loads(run(scenario()))
    assert payload["catalog_version"] == pack(ROLE)["version"]
    assert [topic["id"] for topic in payload["topics"]] == pack(ROLE)["topic_order"]


def test_the_agent_tool_call_is_filled_from_mcp_and_the_plan_is_normalized():
    """The S03 seam end-to-end: model asks for the tool, MCP answers, server gate keeps the plan."""
    tool_call = {"name": LIST_TOOL, "args": {}, "id": "call-mcp"}
    plan = {"focus_topic_ids": ["java-concurrency"], "resume_item_indexes": [],
            "evidence_ids": [], "rationale": "先补并发。"}
    model = RecordingModel(responses=[AIMessage(content="", tool_calls=[tool_call]),
                                      AIMessage(content=json.dumps(plan, ensure_ascii=False))])

    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20) as catalog:
            agent, guard = build_planner(model, scope(), catalog=catalog, structured=False)
            state = await agent.ainvoke({"messages": [HumanMessage(content="请规划")]})
            return state, guard

    state, guard = run(scenario())
    assert guard.catalog_failures == []
    result = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    payload = json.loads(result.content)
    assert payload["catalog_version"] == pack(ROLE)["version"]
    assert [topic["id"] for topic in payload["topics"]] == pack(ROLE)["topic_order"]
    assert result.content.count("base") == 0  # answers never cross this channel


def test_a_broken_catalog_is_recorded_not_silently_planned():
    tool_call = {"name": LIST_TOOL, "args": {}, "id": "call-mcp"}
    model = RecordingModel(responses=[AIMessage(content="", tool_calls=[tool_call]),
                                      AIMessage(content='{"focus_topic_ids": []}')])

    async def scenario():
        async with McpCatalogClient(ROLE, timeout=20,
                                    server_params=fixture_params("bad_payload.py")) as catalog:
            # Entering is fine; the payload check only runs on the call.
            agent, guard = build_planner(model, scope(), catalog=catalog, structured=False)
            await agent.ainvoke({"messages": [HumanMessage(content="请规划")]})
            return guard

    guard = run(scenario())
    assert guard.catalog_failures == ["mcp_bad_response"]
