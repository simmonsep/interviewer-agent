"""Test fixture: correct tool names and schemas, but a catalog version/digest from elsewhere."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("wrong-catalog")


@mcp.tool()
def list_builtin_interview_topics(role: str) -> dict:
    return {"role": role, "catalog_version": "ja05-role-pack-v0", "sha256": "0" * 64,
            "topics": []}


@mcp.tool()
def get_builtin_interview_topic(role: str, topic_id: str) -> dict:
    return {"role": role, "catalog_version": "ja05-role-pack-v0", "sha256": "0" * 64,
            "topic": {"id": topic_id, "title": "x", "url": "https://example.invalid/x"}}


mcp.run(transport="stdio")
