"""Provider-neutral messages: no credentials, thinking blocks or framework task objects."""

from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue

from interview_agent.contracts.common import Contract
from interview_agent.contracts.runtime import ModelProfile, RunEnvelope, ToolName

CallId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]


class ScopeError(ValueError):
    def __init__(self, code: str = "resource_unavailable", diagnostics: dict | None = None):
        self.code = code
        # Shape only: never the model output itself (see runtime.deepseek).
        self.diagnostics = diagnostics or {}
        super().__init__(code)


class ToolCall(Contract):
    id: CallId
    name: Annotated[str, Field(min_length=1, max_length=80)]
    arguments: dict[str, JsonValue]


class AssistantResponse(Contract):
    text: Annotated[str, Field(max_length=8000)] = ""
    tool_calls: Annotated[list[ToolCall], Field(max_length=64)] = []


class Message(Contract):
    role: Literal["user", "assistant", "tool"]
    text: str
    tool_calls: list[ToolCall] = []
    tool_call_id: CallId | None = None
    tool_name: str | None = None
    is_error: bool = False


class ToolSpec(Contract):
    name: ToolName
    description: str
    parameters: dict[str, JsonValue]


class Provider(Protocol):
    profile: ModelProfile

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantResponse: ...


class SealedCheckpoint(Contract):
    payload: Annotated[str, Field(max_length=131072)]
    signature: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RunResult(Contract):
    envelope: RunEnvelope
    checkpoint: SealedCheckpoint | None
    cleanup_complete: bool
