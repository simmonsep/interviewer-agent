"""Test fixture: a catalog server that also declares an extra tool (S04 schema whitelist)."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("extra-tool-catalog")


@mcp.tool()
def list_builtin_interview_topics(role: str) -> dict:
    return {"role": role, "catalog_version": "ja05-role-pack-v1", "sha256": "0", "topics": []}


@mcp.tool()
def get_builtin_interview_topic(role: str, topic_id: str) -> dict:
    return {"role": role, "topic_id": topic_id}


@mcp.tool()
def read_reference_answer(role: str) -> str:
    return "not allowed"


mcp.run(transport="stdio")
