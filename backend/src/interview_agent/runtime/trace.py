"""Bounded allowlisted event records. Raw messages/arguments/quotes are excluded."""

from typing import Literal, Protocol

from interview_agent.contracts.common import Contract, NonNegativeInt
from interview_agent.contracts.runtime import Termination, ToolName

from .types import ScopeError


class TraceRecord(Contract):
    sequence: NonNegativeInt
    event: Literal["start", "model", "tool", "result"]
    tool_name: ToolName | None = None
    is_error: bool | None = None
    termination: Termination | None = None


class TraceSink(Protocol):
    async def append(self, record: TraceRecord) -> None: ...
    async def close(self) -> None: ...


class MemoryTrace:
    def __init__(self):
        self.records: list[TraceRecord] = []
        self.closed = False

    async def append(self, record: TraceRecord) -> None:
        if self.closed or len(self.records) >= 128:
            raise ScopeError("trace_write_failed")
        self.records.append(record.model_copy(deep=True))

    async def close(self) -> None:
        self.closed = True
