"""Committed status notifications; SQL snapshots remain authoritative."""

import asyncio
import json
import re
from time import monotonic

from interview_agent.storage.errors import ApiError

SETTLED = {"SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CREDENTIALS"}


def event_cursor(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value) or int(value) > 9223372036854775807:
        raise ApiError("invalid_event_cursor", 400)
    return int(value)


def frame(name, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return prefix + f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def operation_stream(repository, user, operation_id, after, *, interval=0.5, lifetime=25):
    # No DB connection or transaction is held across yields / sleeps.
    deadline = monotonic() + lifetime
    yield "retry: 1000\n\n"
    try:
        while True:
            state = await repository.get(user, operation_id)
            batch = await repository.events(user, operation_id, after)
            for item in batch["items"]:
                after = int(item["event_id"])
                yield frame("operation", {"operation_id": operation_id, **item}, item["event_id"])
            if state.status in SETTLED and len(batch["items"]) < 100:
                yield frame("stream-end", {"reason": "settled"})
                return
            if monotonic() >= deadline:
                # Browser reconnects with Last-Event-ID. Periodic close bounds stream lifetime.
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(interval)
    except ApiError as error:
        yield frame("auth-expired" if error.status == 401 else "unavailable", {"code": error.code})
    except Exception:
        # Headers have already been sent: do not leak a DB exception or pretend it is HTTP 401.
        yield frame("stream-error", {"code": "event_stream_unavailable"})
