"""Toggleable per-note pipeline flow trace (config `analysis_trace`).

Distinct from `analysis.trace`, which builds the persisted review-card explanation
for a single HELD fact. This one is live operator lighting: when on, each seam of the
graph write path emits ONE structured INFO event keyed by note_id — vision (the
OCR/caption an attachment produced) → per-fact commit decision — so an operator tailing
the worker logs can watch a single note's facts flow end to end, from what the image
model read to where an edge is dropped, refreshed, or superseded.

The three middle seams (extract / intent / plan) went with `integrate_note` in R4: they
traced the model-side chain that read a note, and the note conversation's own reading is
visible in the agent run log instead.

OFF by default and cheap when off: every emitter checks one cached flag before
touching its arguments, so the hot path pays nothing in production. This module is
pure observability — it reads pipeline state and never changes a disposition.

The flag is read once per process (the env value is fixed for a worker's life), so
an operator turns tracing on by setting JBRAIN_ANALYSIS_TRACE=true and restarting
the worker. Tests flip it with `set_enabled` / `reset`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from jbrain.config import get_settings

if TYPE_CHECKING:
    from jbrain.analysis.supersession import Decision, FactView

log = structlog.get_logger()

_enabled: bool | None = None


def enabled() -> bool:
    """Whether per-note flow tracing is on. Cached after the first read: both
    flags are env-fixed for the process, so this avoids re-parsing settings per
    fact.

    Auto-arms whenever the debug console is enabled: that gate is the only way
    these logs get read and is the prerequisite for minting an assistant's debug
    token, so an enabled console IS the debugging session this trace exists for.
    The explicit `analysis_trace` flag stays as an override for tracing without
    the console."""
    global _enabled
    if _enabled is None:
        s = get_settings()
        _enabled = s.analysis_trace or s.debug_access_enabled
    return _enabled


def reset() -> None:
    """Drop the cached flag so the next `enabled()` re-reads the environment
    (tests only; production reads once and never flips mid-process)."""
    global _enabled
    _enabled = None


def set_enabled(value: bool) -> None:
    """Force the cached flag without touching the environment (tests only)."""
    global _enabled
    _enabled = value


# The vision text can be long (a full screenshot transcription); cap it so a
# trace line stays readable while still showing what the image model actually read.
_VISION_TEXT_CAP = 2000


def _short(value: Any) -> str | None:
    """First segment of a UUID/id — compact and greppable in log lines."""
    if value is None:
        return None
    return str(value).split("-", 1)[0]


def vision(
    attachment_id: Any,
    *,
    note_id: Any,
    kind: str,
    provider: str,
    model: str,
    filename: str,
    text: str,
) -> None:
    """Seam 0 — what vision.ocr / vision.caption produced for an attachment, BEFORE
    it becomes chunks the extractor mines. Logs the verbatim transcription/caption
    (capped) with the model that produced it, so an operator can see exactly what
    the image model read — where a screenshot's app chrome or a tool's own narration
    entered the pipeline — without querying the extract cache. Same debug-console
    gate as the rest of the trace."""
    if not enabled():
        return
    log.info(
        "analysis.flow.vision",
        attachment_id=_short(attachment_id),
        note_id=_short(note_id),
        kind=kind,
        provider=provider,
        model=model,
        filename=filename,
        chars=len(text),
        text=text[:_VISION_TEXT_CAP],
        truncated=len(text) > _VISION_TEXT_CAP,
    )


def _edge(entity_ref: str, predicate: str, qualifier: str, obj: str | None) -> str:
    qual = f".{qualifier}" if qualifier else ""
    arrow = f" -> {obj}" if obj else ""
    return f"{entity_ref}.{predicate}{qual}{arrow}"


def _verb(decision: Decision) -> str:
    if decision.refresh_id is not None:
        return "refresh"
    if decision.close_id is not None:
        return "close"
    if decision.insert and decision.supersede_ids:
        return "insert+supersede"
    if decision.insert:
        return "insert"
    return "noop"


def commit(
    note_id: str,
    *,
    entity_ref: str,
    predicate: str,
    qualifier: str,
    object_ref: str | None,
    subject_id: Any,
    object_id: Any,
    existing: list[FactView],
    decision: Decision,
) -> None:
    """Seam 5 — the per-fact commit decision against the resolved graph. `existing`
    is exactly what the identity-key lookup returned, so an enumerated edge that
    pulls back a sibling's row (or resolves to refresh/supersede instead of insert)
    is the collapse made visible at the moment it happens."""
    if not enabled():
        return
    log.info(
        "analysis.flow.commit",
        note_id=note_id,
        edge=_edge(entity_ref, predicate, qualifier, object_ref),
        subject_id=_short(subject_id),
        object_id=_short(object_id),
        verb=_verb(decision),
        existing=[
            {"id": _short(e.id), "obj": _short(e.object_entity_id), "status": e.status}
            for e in existing
        ],
        supersedes=len(decision.supersede_ids),
        holds=len(decision.hold_ids),
        insert_status=decision.insert_status if decision.insert else None,
    )
