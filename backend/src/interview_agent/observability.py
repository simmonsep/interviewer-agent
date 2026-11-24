"""AgentOps trace: default-off, metadata-only, local per-process JSONL, fail-open.

The trace answers "what happened, in what order, and how long did each stage take".
It never decides anything: state transitions, budgets, permissions, retries and
idempotency stay in the deterministic layers. Losing a trace record cannot change a
business outcome, which is why every path here swallows its own failures.

Design constraints (S00O):
- Off unless `INTERVIEW_AGENTOPS` is truthy. Off costs one function call per event.
- The OpenTelemetry SDK is imported lazily, so importing this module is free and a
  frozen build needs it as a hidden import (see scripts/build-desktop-product.ps1).
- Only whitelisted metadata leaves the process. No answer text, no prompt, no tool
  arguments, no exception messages, no headers, no credentials. Unknown keyword
  arguments are dropped instead of stored, so a mistaken caller cannot leak.
- The exporter writes each event through to its file (batching was the first design, but
  the frozen sidecar is terminated without running `atexit`, so buffered events were lost).
  A full buffer, an unwritable directory or a serialisation failure increments `dropped`
  and returns success, because the alternative (blocking or failing a worker) is worse.

Money is NOT recorded here. The transport stores the ledger `model_call_id` and the
query command reads the settled amount from `app.model_http_calls`, so there is one
source of truth instead of two numbers that can disagree.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "agentops-v1"

# Fields allowed to leave the process. Anything else a caller passes is dropped.
FIELDS = frozenset({
    # identity of the running build
    "process", "deployment_commit", "mode", "pack_version", "policy_version",
    "provider", "prompt_id",
    # correlation IDs (server-derived only)
    "session_id", "goal_id", "interview_run_id", "operation_id", "job_id",
    "attempt", "fencing_token", "model_call_id",
    # business shape
    "question_id", "topic_id", "evidence_ids", "question_count", "round_index",
    "tool_name", "tool_status", "tool_count", "error_category", "action", "label", "outcome",
    # transport / budget shape
    "http_status", "request_bytes", "response_bytes", "usage_status",
    "reserved_nano", "accounted_nano", "context_bytes", "truncated_fields",
})

_MAX_TEXT = 160
_MAX_ITEMS = 8

# Bounded failure classes for aggregation. The raw code stays in `reason_code`;
# this is only a stable bucket so "how often did the provider fail" is answerable
# without parsing free text.
_CATEGORY_RULES = (
    ("budget", ("model_budget_exhausted", "model_authorization_unavailable", "model_authorization_mismatch",
                "model_usage_unknown", "model_execution_expired", "model_context_changed",
                "model_not_enabled", "model_credentials_unavailable", "interview_resume_requires_new_input")),
    ("provider", ("model_payload_rejected", "invalid_provider_tool_calls", "invalid_provider_reasoning",
                  "model_response_too_large", "http_error", "usage_unavailable", "usage_invalid",
                  "usage_outside_reservation", "interview_provider_or_graph_error", "invalid_interview_response",
                  "request_outcome_unknown")),
    ("tool", ("interview_tool_not_allowed", "invalid_interview_tool", "read_topics_before_submission",
              "scope_unread", "read_scope_then_separate_submission", "invalid_topic_scope",
              "duplicate_interview_call_id", "invalid_interview_decision_schema")),
    ("guardrail", ("coaching_policy_violation", "question_limit_finish_required", "minimum_coverage_not_met",
                   "question_coverage_required", "question_already_asked", "followup_requires_same_topic_answer",
                   "question_has_no_reference_for_assessment", "bank_question_text_is_frozen",
                   "project_question_not_enabled", "observation_required", "answer_missing", "invalid_finish",
                   "summary_only_at_finish", "invalid_project_question", "fact_not_in_scope",
                   "project_focus_already_asked", "question_not_in_pack", "retest_requires_prior_topic",
                   "explanation_not_allowed", "new_question_required", "practice_mistake_first",
                   "practice_mistake_needs_explanation", "answer_citation_required",
                   "reference_citation_required")),
    ("input", ("idempotency_conflict", "version_conflict", "question_changed", "invalid_request",
               "fact_not_in_application_package", "material_scope_too_large_choose_fact_ids",
               "draft_stale", "choose_draft_or_facts", "alignment_fact_scope_mismatch")),
    ("queue", ("malformed_message", "claim_not_granted", "run_unavailable", "session_not_runnable",
               "lease_held", "lease_lost", "heartbeat_rejected", "heartbeat_failed")),
    ("persistence", ("resource_unavailable", "interview_claim_expired", "interview_failed",
                     "worker_attempt_error")),
    ("cancelled", ("interview_cancelled", "interview_cancelled_or_timeout", "user_stop", "deadline",
                   "attempt_limit", "interview_model_limit", "interview_not_ready")),
)


def category_of(code):
    """Map a stable error code to one bounded class, or 'unknown'."""
    if not isinstance(code, str) or not code:
        return "unknown"
    for name, codes in _CATEGORY_RULES:
        if code in codes:
            return name
    return "unknown"


def _coerce(value):
    """Return a JSON-safe, bounded copy of an allowed value, or None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -2**63 <= value < 2**63 else None
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, (list, tuple)):
        items = [item for item in (_coerce(v) for v in list(value)[:_MAX_ITEMS]) if item is not None]
        return items or None
    return None


def _attributes(stage, identity, fields):
    attributes = {"stage": _coerce(stage)}
    for source in (identity, fields):
        for key, value in source.items():
            if key not in FIELDS and key != "reason_code":
                continue
            cleaned = _coerce(value)
            if cleaned is not None:
                attributes[key] = cleaned
    return {k: v for k, v in attributes.items() if v is not None}


class NullTracer:
    """Default tracer: no SDK import, no I/O, no allocation beyond the call."""

    enabled = False
    dropped = 0

    @contextmanager
    def span(self, event_name, *, stage="unspecified", **fields):
        yield _NullSpan()

    def record(self, event_name, *, stage="unspecified", **fields):
        return None

    def open(self, event_name, *, stage="unspecified", **fields):
        return _OrphanSpan(None, None)

    def flush(self):
        return 0

    def close(self):
        return None


class _NullSpan:
    def set(self, **fields):
        return None

    def ok(self, **fields):
        return None

    def fail(self, reason_code):
        return None


class _Span:
    def __init__(self, span):
        self._span = span
    def set(self, **fields):
        try:
            for key, value in fields.items():
                if key in FIELDS or key == "reason_code":
                    cleaned = _coerce(value)
                    if cleaned is not None:
                        self._span.set_attribute(key, cleaned)
        except Exception:
            pass

    def ok(self, **fields):
        """Mark a span that finished without an error, with optional final fields."""
        try:
            from opentelemetry.trace import Status, StatusCode
            self._span.set_status(Status(StatusCode.OK))
        except Exception:
            pass
        self.set(**fields)

    def fail(self, reason_code):
        """Mark the span failed with a stable code. Never a message or traceback."""
        try:
            from opentelemetry.trace import Status, StatusCode
            self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            pass
        self.set(reason_code=reason_code)


class _OrphanSpan:
    """A span opened outside a `with` block; the owner must call `end()`."""

    def __init__(self, handle, manager):
        self._handle = handle or _NullSpan()
        self._manager = manager

    def set(self, **fields):
        return self._handle.set(**fields)

    def ok(self, **fields):
        return self._handle.ok(**fields)

    def fail(self, reason_code):
        return self._handle.fail(reason_code)

    def end(self):
        if self._manager is not None:
            manager, self._manager = self._manager, None
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


def _record(span, sequence=None):
    """Build the exported record from a finished span, whitelist only."""
    context = span.get_span_context()
    if not context.is_valid:
        return None
    attributes = dict(span.attributes or {})
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_name": span.name,
        "stage": _coerce(attributes.get("stage")),
        "timestamp": datetime.fromtimestamp(span.start_time / 1e9, tz=timezone.utc).isoformat(timespec="microseconds"),
        "sequence": sequence,
        "duration_ms": round((span.end_time - span.start_time) / 1e6, 3) if span.end_time else None,
        "status": _status(span),
        "reason_code": _coerce(attributes.get("reason_code")),
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
        "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
    }
    for key in FIELDS:
        if key in attributes:
            cleaned = _coerce(attributes[key])
            if cleaned is not None:
                record[key] = cleaned
    return record


def _status(span):
    try:
        from opentelemetry.trace import StatusCode
        code = span.status.status_code
        if code == StatusCode.OK:
            return "ok"
        if code == StatusCode.ERROR:
            return "error"
        return "unset"
    except Exception:
        return "unset"


class JsonlSpanExporter:
    """Bounded in-memory buffer appended to one file per process.

    Not a `SpanExporter` subclass on purpose: the SDK import is optional, so the
    exporter is only constructed when tracing is actually enabled.
    """

    def __init__(self, path, *, max_events=1000, flush_every=1, max_bytes=4 * 1024 * 1024):
        self.path = Path(path)
        self.max_events, self.flush_every, self.max_bytes = max_events, flush_every, max_bytes
        self._buffer = []
        self._lock = threading.Lock()
        self._sequence = 0
        self._handle = None
        self.dropped = 0
        self.written = 0
        self._bytes = self.path.stat().st_size if self.path.is_file() else 0

    def export(self, spans):
        try:
            with self._lock:
                for span in spans:
                    try:
                        self._sequence += 1
                        record = _record(span, self._sequence)
                    except Exception:
                        record = None
                    if record is None:
                        self.dropped += 1
                        continue
                    if len(self._buffer) >= self.max_events:
                        self.dropped += 1
                        continue
                    self._buffer.append(record)
                if len(self._buffer) >= self.flush_every:
                    self._write()
        except Exception:
            self.dropped += 1
        return True

    def _write(self):
        if not self._buffer:
            return
        try:
            payload = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                              for item in self._buffer)
        except (TypeError, ValueError):
            self.dropped += len(self._buffer)
            self._buffer.clear()
            return
        try:
            if self._bytes + len(payload) > self.max_bytes:
                self._rotate()
            handle = self._open()
            handle.write(payload)
            handle.flush()
            self._bytes += len(payload)
            self.written += len(self._buffer)
        except (OSError, ValueError):
            self._close()
            self.dropped += len(self._buffer)
        finally:
            self._buffer.clear()

    def _open(self):
        """Keep one handle per process: opening a file per event costs milliseconds on Windows."""
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8", newline="\n")
        return self._handle

    def _close(self):
        if self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                handle.close()
            except OSError:
                pass

    def _rotate(self):
        """Keep one previous file; total disk use stays bounded at ~2x max_bytes."""
        self._close()
        try:
            if self.path.is_file():
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            pass
        self._bytes = 0

    def flush(self):
        with self._lock:
            self._write()
        return self.written

    def shutdown(self):
        written = self.flush()
        self._close()
        return written


class Tracer:
    """Thin wrapper over one SDK tracer plus the whitelist/identity rules."""

    enabled = True

    def __init__(self, provider, exporter, identity=None):
        self._provider = provider
        self._exporter = exporter
        self.identity = {k: v for k, v in (identity or {}).items() if k in FIELDS and v is not None}
        self._tracer = provider.get_tracer("interview_agent", SCHEMA_VERSION)

    def _base(self):
        """Process identity plus the attempt-level correlation of the current task."""
        ambient = _correlation.get() or {}
        return {**ambient, **self.identity}

    @property
    def dropped(self):
        return self._exporter.dropped

    @property
    def written(self):
        return self._exporter.written

    @property
    def path(self):
        return self._exporter.path

    @contextmanager
    def span(self, event_name, *, stage="unspecified", **fields):
        span = None
        manager = None
        try:
            manager = self._tracer.start_as_current_span(
                event_name, attributes=_attributes(stage, self._base(), fields),
                record_exception=False, set_status_on_exception=False)
            span = manager.__enter__()
        except Exception:
            self._exporter.dropped += 1
            manager = None
        handle = _Span(span) if span is not None else _NullSpan()
        try:
            yield handle
        except BaseException as error:
            code = getattr(error, "code", None)
            handle.fail(code if isinstance(code, str) else type(error).__name__)
            raise
        finally:
            if manager is not None:
                try:
                    manager.__exit__(None, None, None)
                except Exception:
                    self._exporter.dropped += 1

    def record(self, event_name, *, stage="unspecified", **fields):
        with self.span(event_name, stage=stage, **fields):
            pass

    def open(self, event_name, *, stage="unspecified", **fields):
        """Open a span that must be ended explicitly. Used where the call site has its
        own try/finally and a `with` block would mean re-indenting the whole body."""
        try:
            manager = self._tracer.start_as_current_span(
                event_name, attributes=_attributes(stage, self._base(), fields),
                record_exception=False, set_status_on_exception=False)
            span = manager.__enter__()
        except Exception:
            self._exporter.dropped += 1
            return _OrphanSpan(None, None)
        return _OrphanSpan(_Span(span), manager)

    def flush(self):
        try:
            return self._exporter.flush()
        except Exception:
            return 0

    def close(self):
        try:
            self._exporter.flush()
            self._provider.shutdown()
        except Exception:
            pass


_current = NullTracer()

# Attempt-level correlation (session/run/operation/attempt) for the current task.
# Child spans and records inherit it, so a query by session id finds every event of
# that attempt without threading ids through every function signature.
_correlation = contextvars.ContextVar("interview_agent_agentops_correlation", default=None)


@contextmanager
def correlation(**identifiers):
    """Attach correlation ids to everything traced inside this block."""
    cleaned = {key: value for key, value in identifiers.items() if key in FIELDS and value is not None}
    token = _correlation.set(cleaned)
    try:
        yield cleaned
    finally:
        _correlation.reset(token)


def current():
    """The process tracer. Business modules call this; it is a no-op until configured."""
    return _current


def reset():
    """Restore the default-off tracer. Tests use this; production never needs it."""
    global _current
    _dispose(_current)
    _current = NullTracer()
    return _current


def enabled_from_env():
    return os.environ.get("INTERVIEW_AGENTOPS", "").strip().lower() in {"1", "on", "true", "yes"}


def directory_from_env():
    override = os.environ.get("INTERVIEW_AGENTOPS_DIR")
    return Path(override) if override else Path.cwd() / ".private" / "agentops"


def _git_commit():
    """Best-effort build identity. A missing git binary is not an error."""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode(errors="replace").strip() or None


def _unavailable(directory, process, code):
    """Record 'tracing was requested but could not start' without the SDK.

    A silent no-op would look identical to 'tracing is off', which is exactly the
    wrong conclusion for an operator. One metadata-only line, best effort.
    """
    try:
        path = Path(directory) / f"agentops-{process}-{os.getpid()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"schema_version": SCHEMA_VERSION, "event_name": "trace.unavailable", "stage": "agentops",
                  "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "sequence": 0,
                  "duration_ms": 0, "status": "error", "reason_code": code, "process": process}
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, ValueError):
        pass


def _build(process, directory, identity, *, flush_every=1, max_events=1000):
    """Construct the real tracer. Returns NullTracer if anything is unavailable."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        _unavailable(directory, process, "sdk_import_failed")
        return NullTracer()
    try:
        path = Path(directory) / f"agentops-{process}-{os.getpid()}.jsonl"
        exporter = JsonlSpanExporter(path, max_events=max_events, flush_every=flush_every)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        full_identity = {"process": process, **identity}
        full_identity.setdefault("deployment_commit", _git_commit())
        return Tracer(provider, exporter, full_identity)
    except Exception:
        _unavailable(directory, process, "sdk_setup_failed")
        return NullTracer()


def configure(process="api", *, enabled=None, directory=None, **identity):
    """Install the process tracer. Off by default; every failure degrades to no-op."""
    global _current
    if enabled is None:
        enabled = enabled_from_env()
    _dispose(_current)
    if not enabled:
        _current = NullTracer()
        return _current
    tracer = _build(process, directory if directory is not None else directory_from_env(), identity)
    if not tracer.enabled:
        _current = NullTracer()
        return _current
    _current = tracer
    atexit.register(tracer.close)
    return tracer


def _dispose(tracer):
    """Flush and release the previous process tracer; never let that failure escape."""
    if isinstance(tracer, Tracer):
        try:
            tracer.close()
        except Exception:
            pass


@contextmanager
def use(tracer):
    """Temporarily install a tracer (tests and one-off local runs)."""
    global _current
    previous = _current
    _current = tracer or NullTracer()
    try:
        yield _current
    finally:
        _current = previous


def load(directory):
    """Read every trace file in a private directory. Returns (records, unreadable_lines)."""
    rows, bad = [], 0
    for path in sorted(Path(directory).glob("*.jsonl*")):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        bad += 1
        except OSError:
            bad += 1
    return rows, bad


def select(rows, *, session_id=None, run_id=None, operation_id=None):
    chosen = []
    for row in rows:
        if session_id and row.get("session_id") != session_id:
            continue
        if run_id and run_id not in {row.get("interview_run_id"), row.get("operation_id")}:
            continue
        if operation_id and row.get("operation_id") != operation_id:
            continue
        if not (session_id or run_id or operation_id):
            continue
        chosen.append(row)
    chosen.sort(key=lambda item: (item.get("timestamp") or "", item.get("sequence") or 0, item.get("span_id") or ""))
    return chosen


def summarize(rows):
    """Stage order, per-stage time, failure classes and money pointers. No conclusions."""
    stages, events, categories, calls = {}, {}, {}, []
    total = 0.0
    for row in rows:
        stage = row.get("stage") or "unspecified"
        stages[stage] = stages.get(stage, 0) + 1
        events[row.get("event_name")] = events.get(row.get("event_name"), 0) + 1
        total += row.get("duration_ms") or 0.0
        if row.get("status") == "error":
            key = row.get("error_category") or "unknown"
            categories[key] = categories.get(key, 0) + 1
        if row.get("model_call_id"):
            calls.append({"model_call_id": row["model_call_id"], "usage_status": row.get("usage_status"),
                          "reserved_nano": row.get("reserved_nano"), "operation_id": row.get("operation_id")})
    return {"events": len(rows), "total_span_ms": round(total, 3), "stages": stages,
            "event_names": events, "failures_by_category": categories,
            "model_calls": calls, "model_http_calls": len(calls)}


def _ledger_cost(config, operation_ids):
    """Money comes from the ledger, never from the trace. None when not asked or unavailable."""
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from interview_agent.worker.config import WorkerSettings
    import asyncio

    settings = WorkerSettings.load(config)

    async def read():
        async with await AsyncConnection.connect(settings.database.conninfo(), row_factory=dict_row) as conn:
            return await (await conn.execute(
                "SELECT operation_id::text AS operation_id,state,reserved_nano,accounted_nano "
                "FROM app.model_http_calls WHERE operation_id=ANY(%s::uuid[]) ORDER BY created_at,id",
                (sorted(set(operation_ids)),))).fetchall()

    return asyncio.run(read(), loop_factory=asyncio.SelectorEventLoop)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="python -m interview_agent.observability",
                                     description="Query one interview trace by session, run or operation.")
    parser.add_argument("--dir", type=Path, default=None, help="trace directory (default: $INTERVIEW_AGENTOPS_DIR)")
    parser.add_argument("--session", help="session_id (the business id returned by the API)")
    parser.add_argument("--run", help="interview_run_id (one worker attempt)")
    parser.add_argument("--operation", help="operation_id (same as the run id for interviews)")
    parser.add_argument("--cost", action="store_true", help="also read settled money from app.model_http_calls")
    parser.add_argument("--config", type=Path, help="worker/test config JSON required by --cost")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of a table")
    args = parser.parse_args(argv)
    if not (args.session or args.run or args.operation):
        parser.error("give --session, --run or --operation")
    directory = args.dir or directory_from_env()
    rows, bad = load(directory)
    chosen = select(rows, session_id=args.session, run_id=args.run, operation_id=args.operation)
    summary = summarize(chosen)
    summary["directory"] = str(directory)
    summary["unreadable_lines"] = bad
    summary["selected"] = len(chosen)
    if args.cost:
        operations = sorted({r["operation_id"] for r in chosen if r.get("operation_id")})
        if not args.config:
            summary["cost"] = {"status": "not_read", "reason": "pass --config"}
        elif not operations:
            summary["cost"] = {"status": "not_called", "reason": "no operation id in the selected trace"}
        else:
            try:
                rows_cost = _ledger_cost(args.config, operations)
            except Exception as error:
                summary["cost"] = {"status": "unavailable", "reason": type(error).__name__}
            else:
                summary["cost"] = {
                    "status": "unknown" if any(r["state"] != "KNOWN" for r in rows_cost) else ("known" if rows_cost else "not_called"),
                    "http_attempts": len(rows_cost),
                    "accounted_upper_nano": sum(r["accounted_nano"] if r["state"] == "KNOWN" else r["reserved_nano"] for r in rows_cost),
                    "calls": [{**r, "operation_id": str(r["operation_id"])} for r in rows_cost]}
    if args.json:
        print(json.dumps({"summary": summary, "events": chosen}, ensure_ascii=False, indent=2))
        return 0
    print(f"directory {directory}  selected {len(chosen)}/{len(rows)} events  unreadable {bad}")
    for row in chosen:
        print(f"{row.get('timestamp')} {str(row.get('stage')):10} {row.get('event_name'):24} "
              f"{(row.get('duration_ms') or 0):>8.2f}ms {str(row.get('status') or ''):5} "
              f"{(row.get('reason_code') or ''):32} {(row.get('tool_name') or '')}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
