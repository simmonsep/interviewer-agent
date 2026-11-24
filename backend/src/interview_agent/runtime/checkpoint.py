"""Signed, bounded tool-boundary export; HMAC is integrity protection, not encryption."""

import hashlib
import hmac
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from interview_agent.contracts.common import Contract, Id
from interview_agent.contracts.runtime import BudgetState, RunCounts, RunInput

from .budget import fingerprint
from .store import ReadWindow, ScopedAssessmentStore, canonical
from .types import Message, ScopeError, SealedCheckpoint


class SavedState(Contract):
    version: Literal["py-runtime-v1"]
    binding: str
    snapshot_digest: str
    run_id: Id
    messages: Annotated[list[Message], Field(min_length=3, max_length=128)]
    reads: Annotated[list[ReadWindow], Field(max_length=12)]
    budget: BudgetState
    counts: RunCounts


def check_pairing(messages: list[Message], run: RunInput) -> None:
    used: set[str] = set()
    pending: dict[str, str] = {}
    for index, message in enumerate(messages):
        if message.role == "user":
            if index != 0 or pending or message.tool_calls or message.tool_call_id or message.tool_name or message.is_error:
                raise ScopeError("invalid_checkpoint")
        elif message.role == "assistant":
            if pending or message.tool_call_id or message.tool_name or message.is_error:
                raise ScopeError("invalid_checkpoint")
            for call in message.tool_calls:
                if call.id in used or call.name not in run.allowed_tools:
                    raise ScopeError("invalid_checkpoint")
                used.add(call.id)
                pending[call.id] = call.name
        else:
            if (message.tool_calls or message.tool_call_id is None or message.tool_name is None
                    or pending.get(message.tool_call_id) != message.tool_name):
                raise ScopeError("invalid_checkpoint")
            del pending[message.tool_call_id]
    if pending or messages[0].role != "user" or messages[-1].role != "tool":
        raise ScopeError("invalid_checkpoint")


def check_key(key: bytes | None) -> bytes:
    if type(key) is not bytes or len(key) < 32:
        raise ScopeError("checkpoint_key_required")
    return key


def seal(saved: SavedState, run: RunInput, key: bytes | None) -> SealedCheckpoint:
    secret = check_key(key)
    check_pairing(saved.messages, run)
    payload = canonical(saved.model_dump(mode="json"))
    if len(payload.encode()) > 131072:
        raise ScopeError("checkpoint_too_large")
    return SealedCheckpoint(payload=payload, signature=hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest())


def open_checkpoint(sealed: SealedCheckpoint, run: RunInput, store: ScopedAssessmentStore, key: bytes | None) -> SavedState:
    secret = check_key(key)
    try:
        value = SealedCheckpoint.model_validate(sealed.model_dump(mode="json"))
        if len(value.payload.encode()) > 131072:
            raise ScopeError("checkpoint_too_large")
        expected = hmac.new(secret, value.payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, value.signature):
            raise ScopeError("invalid_checkpoint")
        saved = SavedState.model_validate_json(value.payload)
        if saved.binding != fingerprint(run) or saved.snapshot_digest != store.digest or run.parent_run_id != saved.run_id:
            raise ScopeError("checkpoint_scope_mismatch")
        check_pairing(saved.messages, run)
        for read in saved.reads:
            store.verify_read(run, read)
        counts = saved.counts
        if (counts.provider_requests_admitted != saved.budget.provider_requests
                or counts.tool_calls_admitted != saved.budget.tool_calls
                or counts.assistant_turns != len([m for m in saved.messages if m.role == "assistant"])
                or counts.provider_request_attempts < counts.provider_requests_admitted
                or not counts.tool_executions <= counts.tool_calls_admitted <= counts.tool_calls_proposed):
            raise ScopeError("invalid_checkpoint")
        return saved
    except (ValidationError, UnicodeError, TypeError, AttributeError) as exc:
        raise ScopeError("invalid_checkpoint") from exc
