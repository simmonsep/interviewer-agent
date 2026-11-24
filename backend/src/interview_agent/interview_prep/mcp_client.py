"""Client for the local stdio MCP catalog server (S04).

The planning sub-agent's `list_builtin_interview_topics` tool goes through this client: one
fixed command (`python -m interview_agent.interview_prep.mcp_catalog`), a bounded handshake
(`initialize` → `list_tools` → `call_tool`), a tool/schema whitelist, and a payload check
against the **same** `interviews.catalog.pack(role)` the preparation snapshot was frozen from.

Trust boundary: the server is a child process on the same machine, but its answers are still
untrusted data. The client only accepts the two known tools with the documented schemas, and
only accepts catalog ids/version/digest that match the local frozen pack. A mismatch is a stable
`McpCatalogError`, never a silently degraded catalog.

Every failure is one of these stable codes (they reach the preparation row as
`mcp_catalog_unavailable` + `mcp_code`):

- `mcp_unavailable`      process could not start / connection closed
- `mcp_timeout`          initialize or a call exceeded the deadline
- `mcp_protocol_error`   JSON-RPC level error from the SDK
- `mcp_bad_response`     tool result was not the documented JSON object
- `mcp_tool_error`       the server returned `isError` (bad role/topic)
- `mcp_tool_schema_mismatch` declared tools/schemas are not the expected pair
- `mcp_catalog_mismatch` role/version/digest/topic ids differ from the local pack
- `mcp_role_not_in_catalog`  the frozen role is not one of the known packs
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from interview_agent.interviews.catalog import ROLES, pack

SERVER_MODULE = "interview_agent.interview_prep.mcp_catalog"
LIST_TOOL = "list_builtin_interview_topics"
GET_TOOL = "get_builtin_interview_topic"
EXPECTED_TOOLS = {LIST_TOOL: {"role"}, GET_TOOL: {"role", "topic_id"}}
DEFAULT_TIMEOUT_SECONDS = 10.0
SOURCE_ROOT = Path(__file__).resolve().parents[2]


class McpCatalogError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


def default_server_params():
    """The one entry point. No user configuration is read and no arbitrary command is run.

    A frozen build has one executable and no source tree: the catalog is then a subcommand of
    this process (see `desktop/sidecar.py`), so the same fixed entry point works in both forms.
    """
    if getattr(sys, "frozen", False):
        return StdioServerParameters(command=sys.executable, args=["mcp-catalog"], env=None)
    existing = os.environ.get("PYTHONPATH")
    env = {"PYTHONPATH": os.pathsep.join([str(SOURCE_ROOT), existing] if existing
                                         else [str(SOURCE_ROOT)])}
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", SERVER_MODULE], env=env)


def expected_catalog(role):
    """Local projection of the frozen pack: what the server must agree with."""
    if role not in ROLES:
        raise McpCatalogError("mcp_role_not_in_catalog", {"role": str(role)[:40]})
    value = pack(role)
    return {"role": role, "version": value["version"], "sha256": value["sha256"],
            "topic_ids": list(value["topic_order"])}


def _classify(error):
    if isinstance(error, TimeoutError):
        return "mcp_timeout"
    if isinstance(error, McpError):
        text = str(getattr(error, "error", error)).lower()
        if "timeout" in text:
            return "mcp_timeout"
        if "closed" in text or "connection" in text:
            return "mcp_unavailable"
        return "mcp_protocol_error"
    if isinstance(error, (OSError, ConnectionError, EOFError, asyncio.IncompleteReadError)):
        return "mcp_unavailable"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "mcp_bad_response"
    return "mcp_unavailable"


def _payload(result):
    """Prefer structured content, fall back to the JSON text block FastMCP also emits."""
    value = getattr(result, "structuredContent", None)
    if isinstance(value, dict) and value:
        return value
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise McpCatalogError("mcp_bad_response", {"reason": "no_json_object"})


def _clean_topic(topic):
    if not isinstance(topic, dict):
        raise McpCatalogError("mcp_bad_response", {"reason": "topic_not_an_object"})
    identity, title, url = topic.get("id"), topic.get("title"), topic.get("url")
    if not (isinstance(identity, str) and 0 < len(identity) <= 80):
        raise McpCatalogError("mcp_bad_response", {"reason": "bad_topic_id"})
    if not (isinstance(title, str) and 0 < len(title) <= 200):
        raise McpCatalogError("mcp_bad_response", {"reason": "bad_topic_title"})
    if not (isinstance(url, str) and url.startswith("https://") and len(url) <= 500):
        raise McpCatalogError("mcp_bad_response", {"reason": "bad_topic_url"})
    return {"id": identity, "title": title, "url": url}


def _validate_envelope(value, expected):
    """Required metadata must be present and well typed before values are compared."""
    if not isinstance(value, dict):
        raise McpCatalogError("mcp_bad_response", {"reason": "not_an_object"})
    for field in ("role", "catalog_version", "sha256"):
        if not isinstance(value.get(field), str) or not value.get(field):
            raise McpCatalogError("mcp_bad_response", {"reason": f"missing_{field}"})
    for field, wanted in (("role", expected["role"]),
                          ("catalog_version", expected["version"]),
                          ("sha256", expected["sha256"])):
        if value[field] != wanted:
            raise McpCatalogError("mcp_catalog_mismatch", {"field": field})


def _validate_payload(value, expected):
    """Only the documented metadata may pass, and it must describe the local frozen pack."""
    _validate_envelope(value, expected)
    topics = value.get("topics")
    if not isinstance(topics, list):
        raise McpCatalogError("mcp_bad_response", {"reason": "topics_not_a_list"})
    cleaned = [_clean_topic(topic) for topic in topics]
    if [topic["id"] for topic in cleaned] != expected["topic_ids"]:
        raise McpCatalogError("mcp_catalog_mismatch", {"field": "topic_ids"})
    return {"role": expected["role"], "catalog_version": expected["version"],
            "sha256": expected["sha256"], "topics": cleaned}


class McpCatalogClient:
    """One short-lived stdio session per preparation attempt. Always closed by the caller."""

    def __init__(self, role, *, timeout=DEFAULT_TIMEOUT_SECONDS, server_params=None):
        self.role = role
        self.timeout = timeout
        self.expected = expected_catalog(role)
        self.params = server_params or default_server_params()
        self._stack = None
        self._session = None
        self._closed = False

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        try:
            async with asyncio.timeout(self.timeout):
                read, write = await self._stack.enter_async_context(stdio_client(self.params))
                self._session = await self._stack.enter_async_context(
                    ClientSession(read, write,
                                  read_timeout_seconds=timedelta(seconds=self.timeout)))
                await self._session.initialize()
                declared = (await self._session.list_tools()).tools
                self._validate_tools(declared)
            return self
        except McpCatalogError:
            await self.aclose()
            raise
        except Exception as error:
            await self.aclose()
            raise McpCatalogError(_classify(error), {"stage": "initialize"}) from None

    def _validate_tools(self, tools):
        found = {}
        for tool in tools or []:
            schema = getattr(tool, "inputSchema", None) or {}
            properties = schema.get("properties") if isinstance(schema, dict) else None
            found[getattr(tool, "name", None)] = schema
            if not isinstance(properties, dict):
                raise McpCatalogError("mcp_tool_schema_mismatch",
                                      {"tool": str(getattr(tool, "name", ""))[:80]})
        if set(found) != set(EXPECTED_TOOLS):
            raise McpCatalogError("mcp_tool_schema_mismatch",
                                  {"declared": sorted(str(name)[:80] for name in found)})
        for name, expected_properties in EXPECTED_TOOLS.items():
            schema = found[name]
            properties = schema.get("properties") or {}
            required = schema.get("required") or []
            if (set(properties) != expected_properties
                    or set(required) != expected_properties
                    or schema.get("type") != "object"):
                raise McpCatalogError("mcp_tool_schema_mismatch", {"tool": name})

    async def _call(self, name, arguments):
        if self._session is None or self._closed:
            raise McpCatalogError("mcp_unavailable", {"reason": "session_closed"})
        if name not in EXPECTED_TOOLS:
            raise McpCatalogError("mcp_tool_schema_mismatch", {"tool": name})
        if set(arguments) != EXPECTED_TOOLS[name] or arguments.get("role") != self.expected["role"]:
            # The role always comes from the frozen scope, never from the model.
            raise McpCatalogError("mcp_bad_response", {"reason": "unexpected_arguments"})
        try:
            async with asyncio.timeout(self.timeout):
                result = await self._session.call_tool(name, arguments)
        except McpCatalogError:
            raise
        except Exception as error:
            raise McpCatalogError(_classify(error), {"tool": name}) from None
        if getattr(result, "isError", False):
            raise McpCatalogError("mcp_tool_error", {"tool": name})
        return _payload(result)

    async def list_topics(self):
        value = await self._call(LIST_TOOL, {"role": self.role})
        return _validate_payload(value, self.expected)

    async def get_topic(self, topic_id):
        if not isinstance(topic_id, str) or topic_id not in self.expected["topic_ids"]:
            raise McpCatalogError("mcp_tool_error", {"tool": GET_TOOL})
        value = await self._call(GET_TOOL, {"role": self.role, "topic_id": topic_id})
        _validate_envelope(value, self.expected)
        topic = _clean_topic(value.get("topic"))
        if topic["id"] != topic_id:
            raise McpCatalogError("mcp_catalog_mismatch", {"field": "topic"})
        return {"role": self.expected["role"], "catalog_version": self.expected["version"],
                "sha256": self.expected["sha256"], "topic": topic}

    async def list_topics_json(self):
        """What the planning tool returns to the model: metadata only, compact JSON."""
        return json.dumps(await self.list_topics(), ensure_ascii=False)

    async def aclose(self):
        self._closed = True
        self._session = None
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    async def __aexit__(self, *exc_info):
        await self.aclose()
        return False
