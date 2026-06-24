"""Per-job structured-log capture for the Runs "full logs" review trace.

The worker scopes a `LogScope` around each job; a structlog processor (installed
in the worker's logging chain) taps every event the job emits into the active
scope's buffer, which the worker then writes onto that job's `run_step.detail`. So
an owner can review WHAT a run did — its LLM calls, build/integration events,
errors — not just that it finished.

The processor is a pure pass-through: it copies the event and returns it unchanged,
so normal stdout logging is untouched. Capture is bounded (a chatty job can't grow
the buffer without limit) and JSON-coerced (non-primitive values are stringified)
so the trace is always safe to store in the JSONB column.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

# The active job's captured events; None = no scope, so capture is skipped.
_log_events: ContextVar[list[dict[str, Any]] | None] = ContextVar("log_events", default=None)

# A single chatty job (many LLM calls) must not store an unbounded trace; keep the
# first N events (a run that floods past this is itself the signal). Bounded, not
# tail-only, so the opening context of a run is always present.
_MAX_EVENTS = 500


def _json_safe(value: Any) -> Any:
    """A value the JSONB column can store: primitives pass through, anything else
    (a UUID, an exception repr, a datetime) is stringified."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def capture_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: tap each event into the active LogScope (if any), then
    return it UNCHANGED so the normal renderer still emits it. Pure pass-through."""
    buf = _log_events.get()
    if buf is not None and len(buf) < _MAX_EVENTS:
        buf.append({k: _json_safe(v) for k, v in event_dict.items()})
    return event_dict


class LogScope:
    """Capture the structured-log events emitted in this scope (one worker job).
    Read `events` after the scope's body to persist them on the run step."""

    def __enter__(self) -> LogScope:
        self._buf: list[dict[str, Any]] = []
        self._token = _log_events.set(self._buf)
        return self

    def __exit__(self, *_exc: Any) -> None:
        _log_events.reset(self._token)

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._buf


def configure_logging() -> None:
    """Install the worker's structlog chain WITH the capture tap, so a LogScope can
    record what a job logs. Mirrors the API's config (ISO timestamps + JSON render)
    with `capture_processor` inserted just before the renderer."""
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            capture_processor,
            structlog.processors.JSONRenderer(),
        ]
    )
