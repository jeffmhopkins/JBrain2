"""Drive parsed EMR candidates into the graph (docs/plans/EMR_IMPORT_PLAN.md §6.6).

Lowers a `ParseResult` into per-episode `IntegrationIntent`s and commits each
through the SHIPPED deterministic core — `plan_intent` (validate/weigh/partition)
then `AnalysisPipeline.apply_intent` (resolve entities, per-kind supersession incl.
the §3.5 `_lab_status_transition`, RLS-scoped writes, the kind-guarded projection
hook). One intent per grouping unit commits in its OWN transaction, so a worker
crash re-runs only unfinished units and an out-of-order commit can't FK-fault
(encounter_id/part_of_id reference `app.entities`, written before any projection
row within its own apply).

The EMR facts are all surface-attested (deterministic parse), so every fact gets
`surface_attested=True`; `fhir_status` — not `correction` — drives the lab
lifecycle transition inside `decide`, so `is_supersede` stays False.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.candidates import ParseResult
from jbrain.ingest.emr.importer import ChunkResolver, FirewallCatch, lower_parse_result

_SURFACE = ConfidenceSignals(surface_attested=True, is_supersede=False)
EXTRACTOR = "emr:deterministic"


async def integrate_parse_result(
    pipeline: AnalysisPipeline,
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    captured_at: datetime,
    chunks: list[_ChunkRef],
    result: ParseResult,
    chunk_for_anchor: ChunkResolver,
    title: str = "Medical records",
    tags: list[str] | None = None,
) -> list[FirewallCatch]:
    """Commit a parse result's facts. Each per-episode intent runs in its own
    RLS-scoped transaction (§6.6). Returns the Layer-2 firewall catches (facts
    held out of the graph). Provider resolution runs under `ctx` — pass a
    health-only scope so a general-domain namesake is invisible and a health
    `Person` is minted, not re-matched (§3.6)."""
    intents, catches = lower_parse_result(result, str(note_id), chunk_for_anchor)
    for intent in intents:
        signals = {i: _SURFACE for i in range(len(intent.facts))}
        plan = plan_intent(intent, signals=signals)
        async with scoped_session(maker, ctx) as session:
            await pipeline.apply_intent(
                session,
                note_id=note_id,
                note_domain=note_domain,
                captured_at=captured_at,
                chunks=chunks,
                intent=intent,
                plan=plan,
                title=title,
                tags=tags or [],
                extractor=EXTRACTOR,
            )
    return catches
