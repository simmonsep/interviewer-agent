"""Local stdio MCP server for the built-in interview topic catalog (S04).

One server, two read-only tools, one source of truth: every answer is projected from
`interviews.catalog.pack(role)`, the same frozen role pack the preparation snapshot uses. The
tools return **catalog metadata only** — id, title, source link, role, version and digest. They
never return `base`/`probe`/`retest` or `reference`, so an agent cannot read reference answers
through this channel (the formal interview path keeps its own registered questions).

The process speaks MCP over stdout and writes diagnostics to stderr. `main()` takes no arguments
and no path from configuration: the client starts exactly `python -m
interview_agent.interview_prep.mcp_catalog`.

`INTERVIEW_MCP_LIFECYCLE_FILE` is a test-only seam: when set, the server appends one line at
start and one at shutdown. It is unset in the product, and it never writes to stdout.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from interview_agent.interviews.catalog import ROLES, pack

LIST_TOOL = "list_builtin_interview_topics"
GET_TOOL = "get_builtin_interview_topic"
TOOL_NAMES = (LIST_TOOL, GET_TOOL)
# One source of truth: adding a role to the catalog must not require a second allowlist here.
KNOWN_ROLES = tuple(ROLES)


def _lifecycle(event):
    path = os.environ.get("INTERVIEW_MCP_LIFECYCLE_FILE")
    if not path:
        return
    try:
        with open(Path(path), "a", encoding="utf-8") as handle:
            handle.write(f"{event} {os.getpid()}\n")
    except OSError:
        pass


@asynccontextmanager
async def _lifespan(_server):
    _lifecycle("started")
    try:
        yield {}
    finally:
        _lifecycle("stopped")


mcp = FastMCP("interview-catalog", lifespan=_lifespan)


def _pack(role):
    if role not in KNOWN_ROLES:
        raise ValueError("role_not_in_catalog")
    return pack(role)


def _topic(item_id, item):
    return {"id": item_id, "title": str(item.get("title") or ""),
            "url": str(item.get("url") or "")}


def catalog_payload(role):
    """The complete catalog metadata for one role. Pure function, easy to test."""
    value = _pack(role)
    return {"role": role, "catalog_version": value["version"], "sha256": value["sha256"],
            "topics": [_topic(item_id, value["topics"][item_id])
                       for item_id in value["topic_order"]]}


@mcp.tool(name=LIST_TOOL, description="List this role's built-in interview topics (metadata only).")
def list_builtin_interview_topics(role: str) -> dict:
    return catalog_payload(role)


@mcp.tool(name=GET_TOOL,
          description="One built-in interview topic's metadata (id, title, source link).")
def get_builtin_interview_topic(role: str, topic_id: str) -> dict:
    value = _pack(role)
    if topic_id not in value["topic_order"]:
        raise ValueError("topic_not_in_catalog")
    return {"role": role, "catalog_version": value["version"], "sha256": value["sha256"],
            "topic": _topic(topic_id, value["topics"][topic_id])}


def main():
    # Protocol bytes go to stdout; nothing else in this process may print there.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
