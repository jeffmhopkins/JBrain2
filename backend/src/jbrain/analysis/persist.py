"""Persist an integration run + its resolution pins (Phase 5, Wave 1, Track A).

The Integrator turn-loop logs to structlog only today; this is its net-new
persistence (docs/WORKFLOW_ENGINE_PLAN.md §E7b). Two writes, both gated behind
the `integration_persist` setting and both run under the all-domains SYSTEM_CTX
(the integration pipeline legitimately crosses every firewall, E1) while
recording `ran_as='system'` on the run so the audit shows owner-system, not a
smuggled escalation:

- **the run** — one `app.runs` row per `integrate_note` call, `kind='integration'`,
  carrying the note's domain + the owner/system principal and a deterministic
  step trace (extract -> integrate -> arbiter), so the unified run log holds the
  Integrator alongside the agent;
- **the pins** — the Integrator's committed identity + predicate-key decisions
  built through the PURE `analysis.pins` helpers and UPSERTed into
  `app.resolution_pin` keyed `(note_id, chunk_id, occurrence_index, decision_kind)`.

Because there is no shadow baseline (the loop never persisted before), this is
validated by CONVERGENCE: re-integrating the same note yields the SAME pins
(idempotent upsert, never duplicate rows) and one run per call. The pin builders
are deliberately pure (intent + chunk text in, pins out) so that convergence is a
unit property the integration test only has to confirm against real Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.arbiter import ArbiterPlan
from jbrain.analysis.entities import ResolvedEntity
from jbrain.analysis.intent import IntegrationIntent
from jbrain.analysis.pins import ResolutionPin, build_pin
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import Run, RunStep
from jbrain.models.workflow import ResolutionPin as ResolutionPinRow


class ChunkText(Protocol):
    """The minimal chunk shape the pin builders anchor a surface to (id + text);
    pipeline._ChunkRef satisfies it structurally, so the caller passes its own
    chunk refs with no conversion."""

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def text(self) -> str: ...


# The run's step trace mirrors the deterministic pipeline stages build_trace
# already names (extraction -> integration -> arbiter), so the run log and a
# held-fact card describe the same three stages with one vocabulary.
def build_run_steps(intent: IntegrationIntent, plan: ArbiterPlan) -> list[tuple[str, str, bool]]:
    """The integration run's ordered (kind, name, ok) steps.

    Pure projection of objects the pipeline already holds at commit time: the
    extract stage produced the facts the intent carries, the integrate stage is
    the Integrator's judgment, and the arbiter stage's ok flips False when it
    rejected the whole intent (a fatal structural violation held the note)."""
    return [
        ("extraction", "note.extract", True),
        ("integration", "integrate.note", True),
        ("arbiter", "plan_intent", not plan.rejected),
    ]


def build_pins(
    intent: IntegrationIntent,
    plan: ArbiterPlan,
    chunks: Sequence[ChunkText],
    *,
    resolved: Mapping[str, ResolvedEntity | None],
) -> list[ResolutionPin]:
    """The committed identity + predicate-key decisions as pure ResolutionPins.

    - **identity**: one pin per resolution whose mention COMMITTED to an entity
      (`resolved[ref]` is not None), anchored to the resolution's attested span.
    - **predicate_key**: one pin per fact the arbiter COMMITTED (`plan.to_commit`),
      carrying the (already-canonicalized) predicate, anchored to the fact's span.

    A decision with no pinnable span (no attested surface, or a surface absent /
    zero-width in its chunk) is skipped — `build_pin` returns None, routing it to
    re-decision rather than seeding a cross-talking pin (pins.py N10). Pure: the
    same intent + chunk text always yields the same pins, which is the
    convergence guarantee the upsert relies on."""
    pins: list[ResolutionPin] = []
    note_id = intent.note_id

    for r in intent.entity_resolutions:
        entity = resolved.get(r.mention_ref)
        if entity is None or r.attested_span is None:
            continue
        pin = _build_at(
            note_id=note_id,
            decision_kind="identity",
            surface=r.attested_span.surface,
            chunks=chunks,
            entity_id=str(entity.id),
        )
        if pin is not None:
            pins.append(pin)

    for pf in plan.to_commit:
        fact = pf.fact
        if fact.attested_span is None:
            continue
        pin = _build_at(
            note_id=note_id,
            decision_kind="predicate_key",
            surface=fact.attested_span.surface,
            chunks=chunks,
            normalized_predicate=fact.predicate,
        )
        if pin is not None:
            pins.append(pin)

    # The PK is (note_id, chunk_id, occurrence_index, decision_kind); two committed
    # facts on the same surface span share a predicate_key key, so collapse to the
    # last write rather than emit a duplicate the upsert would silently fold anyway
    # (keeps the in-memory list a faithful preview of the persisted rows).
    deduped: dict[tuple[str, str, int, str], ResolutionPin] = {}
    for pin in pins:
        deduped[(pin.note_id, pin.chunk_id, pin.occurrence_index, pin.decision_kind)] = pin
    return list(deduped.values())


def _build_at(
    *,
    note_id: str,
    decision_kind: str,
    surface: str,
    chunks: Sequence[ChunkText],
    entity_id: str | None = None,
    normalized_predicate: str | None = None,
) -> ResolutionPin | None:
    """Locate `surface`'s first occurrence across the note's chunks and pin the
    decision there. Mirrors the arbiter's own `_locate` ordering (first chunk
    containing the exact surface) so a pin anchors to the SAME span the citation
    does. Returns None when no chunk holds the surface or it is zero-width."""
    for chunk in chunks:
        start = chunk.text.find(surface)
        if start != -1:
            return build_pin(
                note_id=note_id,
                chunk_id=str(chunk.id),
                decision_kind=decision_kind,  # type: ignore[arg-type]
                text=chunk.text,
                surface=surface,
                start=start,
                entity_id=entity_id,
                normalized_predicate=normalized_predicate,
            )
    return None


class IntegrationRunLog:
    """Writes an integration run + its resolution pins. Mirrors agent/runlog.py's
    writer style (own the SQL, take a SessionContext) but is single-shot: an
    integration is one bounded pipeline pass, not an open turn loop, so it writes
    the finished run in one call rather than start/step/finish."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def persist(
        self,
        ctx: SessionContext,
        *,
        note_id: str,
        note_domain: str,
        intent: IntegrationIntent,
        plan: ArbiterPlan,
        chunks: Sequence[ChunkText],
        resolved: Mapping[str, ResolvedEntity | None],
    ) -> str:
        """Write the integration run row + its steps, and UPSERT the committed
        pins. One transaction so a re-run never partial-writes (N5). Returns the
        run id. `ctx` is SYSTEM_CTX (the integration pipeline crosses every
        firewall, E1); `ran_as='system'` records that choice on the run."""
        steps = build_run_steps(intent, plan)
        pins = build_pins(intent, plan, chunks, resolved=resolved)
        async with scoped_session(self._maker, ctx) as session:
            principal_id = await self._owner_principal_id(session)
            run = Run(
                kind="integration",
                ran_as="system",
                domain_code=note_domain,
                principal_id=principal_id,
                status="rejected" if plan.rejected else "done",
                stop_reason="rejected" if plan.rejected else "committed",
                step_count=len(steps),
                # The per-call token usage is already metered into app.llm_usage by
                # the router's recorder; counting it again here would double-count,
                # so the run's cost_tokens stays 0 until the engine threads usage
                # through explicitly (a later Track-A item).
                cost_tokens=0,
                ended_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            for idx, (kind, name, ok) in enumerate(steps):
                session.add(RunStep(run_id=run.id, idx=idx, kind=kind, name=name, ok=ok))
            await self._upsert_pins(session, note_id=note_id, note_domain=note_domain, pins=pins)
            return str(run.id)

    async def _upsert_pins(
        self,
        session: AsyncSession,
        *,
        note_id: str,
        note_domain: str,
        pins: list[ResolutionPin],
    ) -> None:
        """UPSERT each pin on its PK so a re-run converges to the SAME rows rather
        than duplicating, then DELETE any stale pin this run no longer asserts (an
        edit can drop a mention). The upsert leaves an unchanged decision's row
        untouched (no created_at churn); the targeted delete keeps the table a
        faithful projection of the latest decisions without wiping the rows the
        re-run still holds. Idempotent because the pin SET is deterministic for
        unchanged text."""
        for pin in pins:
            stmt = pg_insert(ResolutionPinRow).values(
                note_id=uuid.UUID(pin.note_id),
                chunk_id=uuid.UUID(pin.chunk_id),
                occurrence_index=pin.occurrence_index,
                decision_kind=pin.decision_kind,
                surface=pin.surface,
                span_text_hash=pin.span_text_hash,
                entity_id=uuid.UUID(pin.entity_id) if pin.entity_id else None,
                normalized_predicate=pin.normalized_predicate,
                domain_code=note_domain,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="resolution_pin_pkey",
                    set_={
                        "surface": stmt.excluded.surface,
                        "span_text_hash": stmt.excluded.span_text_hash,
                        "entity_id": stmt.excluded.entity_id,
                        "normalized_predicate": stmt.excluded.normalized_predicate,
                        "domain_code": stmt.excluded.domain_code,
                    },
                )
            )

        # Retire pins this run no longer asserts (a dropped mention / edited span).
        # A row-value match on the surviving PK tails so a still-asserted pin is
        # never collaterally deleted; no surviving set means the note has no
        # committed decisions this run, so all its pins go. The tuple comparison is
        # typed (no string-concat coupling) and index-friendly.
        stale = delete(ResolutionPinRow).where(ResolutionPinRow.note_id == uuid.UUID(note_id))
        survivors = [
            (uuid.UUID(pin.chunk_id), pin.occurrence_index, pin.decision_kind) for pin in pins
        ]
        if survivors:
            key = tuple_(
                ResolutionPinRow.chunk_id,
                ResolutionPinRow.occurrence_index,
                ResolutionPinRow.decision_kind,
            )
            stale = stale.where(key.not_in(survivors))
        await session.execute(stale)

    @staticmethod
    async def _owner_principal_id(session: AsyncSession) -> uuid.UUID:
        """The ACTIVE owner/system principal the integration run is stamped to.
        SYSTEM_CTX is owner-kind, so is_owner() RLS lets it read app.principals
        (0001). A rotated owner leaves revoked rows behind (revoked_at set), so
        scope to the live one — there is exactly one active owner at a time."""
        pid = (
            await session.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
            )
        ).scalar_one()
        return pid
