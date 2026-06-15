"""Event emission at the hardcoded trigger points (Phase-5 Wave-1 Track A, shadow).

The dispatcher (workflow/dispatcher.py) fans `app.events` rows out to triggers;
this module is the *producer* side. Today's three hardcoded trigger points —
note-created (`api/notes.py`), ingest-done (`ingest/pipeline.py`), and
resolution-changed (`analysis/repo.py`) — each `enqueue` a job. Wave 1 adds an
ADDITIVE event emission ALONGSIDE that enqueue: the hardcoded path still owns the
real work this wave (Wave 2 cuts over), so an event is a shadow observation, never
a second enqueue.

Two invariants make this safe to bolt onto the live path:

- **Inert on failure.** Emission opens its OWN scoped session and swallows every
  error: a missing owner principal, an FK hiccup, anything. A shadow observation
  must NEVER break note creation, ingest, or a resolution — today's behavior is
  the contract (E7a). So this is best-effort by design, logged (`event.emit_*`),
  not raised.
- **E2 fail-closed domain stamp.** Every event carries the most-restrictive
  `domain_code` the triggering content touched (the note's domain). `events` is a
  domain-firewalled table, so an emit under the owner/SYSTEM context (which sees
  every domain) is the only context that can write an event for an arbitrary
  domain; a narrowed caller can only emit for its own domain — RLS enforces it.

The `_shadow_enqueued` payload key records what the hardcoded path actually
enqueued for this event (kind + payload). The dispatcher diffs its WOULD-enqueue
against that recorded baseline (E7a) without correlating rows across `app.jobs` —
the baseline travels on the event itself, so the diff is a pure within-event
comparison even after the originating job has aged out (N2).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.db.session import SessionContext, scoped_session

log = structlog.get_logger()

# The three event types the hardcoded trigger points emit. Free text by design
# (events have no type table); the seeded triggers (migration 0040) bind these
# exact strings to their one-action pipelines.
NOTE_CREATED = "note.created"
NOTE_INGESTED = "note.ingested"
RESOLUTION_CHANGED = "resolution.changed"

# Reserved payload key carrying the hardcoded path's actual enqueue, so the
# dispatcher can diff its would-be enqueue against the real one (E7a) without a
# cross-table job lookup.
SHADOW_ENQUEUED_KEY = "_shadow_enqueued"


async def _resolve_owner_principal(session: AsyncSession) -> str | None:
    """The owner principal's id, for an event emitted from a system/worker context
    that has no per-content principal (ingest-done, resolution under SYSTEM_CTX).

    `events.principal_id` is a real FK to `app.principals`, but the worker's
    SYSTEM_CTX principal ("worker") is not a row there. The single-owner system has
    exactly one owner principal; resolve it so the event names a real identity the
    dispatcher can narrow a scope from (E1). None when no owner exists yet (a fresh
    DB, or a test that never bootstrapped one) — the caller then skips the emit
    rather than violating the FK, keeping emission inert."""
    row = (
        await session.execute(
            text(
                "SELECT id::text FROM app.principals WHERE kind = 'owner'"
                " ORDER BY created_at LIMIT 1"
            )
        )
    ).first()
    return row.id if row is not None else None


def shadow_enqueued(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The `_shadow_enqueued` baseline descriptor for one hardcoded enqueue: the
    job `kind` the path produced and the payload it carried. The dispatcher's diff
    target (E7a)."""
    return {"kind": kind, "payload": payload}


async def emit_event(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    type: str,
    domain_code: str,
    payload: dict[str, Any] | None = None,
    enqueued: dict[str, Any] | None = None,
    principal_id: str | None = None,
) -> str | None:
    """Best-effort insert of one `app.events` row alongside a hardcoded enqueue.

    `domain_code` is the fail-closed E2 stamp (the triggering content's domain).
    `principal_id` is the triggering identity; when None it resolves to the owner
    principal (the worker/system trigger points have no per-content principal).
    `enqueued` records what the hardcoded path actually enqueued so the dispatcher
    can diff against it.

    Returns the event id, or None when emission was skipped/failed. NEVER raises:
    a shadow emission must not break the live path it observes (E7a)."""
    body = dict(payload or {})
    if enqueued is not None:
        body[SHADOW_ENQUEUED_KEY] = enqueued
    event_id = str(uuid.uuid4())
    try:
        async with scoped_session(maker, ctx) as session:
            principal = principal_id or await _resolve_owner_principal(session)
            if principal is None:
                log.info("event.emit_skipped", type=type, reason="no owner principal")
                return None
            await session.execute(
                text(
                    "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                    " VALUES (:id, :type, cast(:payload AS jsonb), :domain, :principal)"
                ),
                {
                    "id": event_id,
                    "type": type,
                    "payload": json.dumps(body),
                    "domain": domain_code,
                    "principal": principal,
                },
            )
    except Exception as exc:  # noqa: BLE001 - a shadow emit must never break the live path
        log.warning("event.emit_failed", type=type, domain=domain_code, error=repr(exc))
        return None
    log.info("event.emitted", event_id=event_id, type=type, domain=domain_code)
    return event_id


# Re-export for callers that emit from the worker/system trigger points and want
# the SYSTEM context explicit at the call site.
SYSTEM_CTX = queue.SYSTEM_CTX
