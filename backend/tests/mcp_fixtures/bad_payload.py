"""Test fixture: valid RPC, valid tool names, but the payload is not catalog metadata."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("bad-payload")


@mcp.tool()
def list_builtin_interview_topics(role: str) -> dict:
    return {"hello": "world"}


@mcp.tool()
def get_builtin_interview_topic(role: str, topic_id: str) -> dict:
    return {"hello": "world"}


mcp.run(transport="stdio")
