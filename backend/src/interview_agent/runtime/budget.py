"""Admission before I/O, atomic batch reservation and non-resettable deadlines."""

import hashlib
import time

from interview_agent.contracts.runtime import BudgetState, RunInput

from .store import canonical
from .types import ScopeError


def fingerprint(run: RunInput) -> str:
    value = run.model_dump(mode="json", exclude={"parent_run_id"})
    value["allowed_tools"] = sorted(value["allowed_tools"])
    value["context_refs"] = sorted(value["context_refs"], key=lambda r: (r["source_id"], r["version"]))
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class OperationBudget:
    def __init__(self, run: RunInput):
        self.binding = fingerprint(run)
        self.limits = run.budget.model_copy(deep=True)
        self.used = BudgetState(turns=0, provider_requests=0, tool_calls=0,
                                deadline_epoch_ms=int(time.time() * 1000) + run.budget.wall_ms)
        self._monotonic_end = time.monotonic() + run.budget.wall_ms / 1000

    def remaining(self) -> float:
        return max(0.0, min(self._monotonic_end - time.monotonic(), self.used.deadline_epoch_ms / 1000 - time.time()))

    def request(self) -> str | None:
        if self.remaining() <= 0:
            return "wall_deadline"
        if self.used.turns >= self.limits.turns:
            return "turn_budget"
        if self.used.provider_requests >= self.limits.provider_requests:
            return "request_budget"
        self.used.turns += 1
        self.used.provider_requests += 1
        return None

    def batch(self, size: int) -> bool:
        if type(size) is not int or size < 1 or self.used.tool_calls + size > self.limits.tool_calls:
            return False
        self.used.tool_calls += size
        return True

    def restore(self, saved: BudgetState) -> None:
        for field, limit in [("turns", self.limits.turns), ("provider_requests", self.limits.provider_requests),
                             ("tool_calls", self.limits.tool_calls)]:
            if getattr(saved, field) > limit:
                raise ScopeError("invalid_checkpoint")
            setattr(self.used, field, max(getattr(self.used, field), getattr(saved, field)))
        self.used.deadline_epoch_ms = min(self.used.deadline_epoch_ms, saved.deadline_epoch_ms)
        self._monotonic_end = min(self._monotonic_end, time.monotonic() + max(0, saved.deadline_epoch_ms / 1000 - time.time()))
