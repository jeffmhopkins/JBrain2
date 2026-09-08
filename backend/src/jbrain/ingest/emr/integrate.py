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
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.candidates import ParseResult
from jbrain.ingest.emr.firewall import FIREWALL_REVIEW_KIND
from jbrain.ingest.emr.importer import ChunkResolver, FirewallCatch, lower_parse_result
from jbrain.ingest.emr.pathology import extract_pathology_diagnoses
from jbrain.ingest.emr.reconcile import REVIEW_KIND, ParkedRead
from jbrain.models.analysis import ReviewItem

_SURFACE = ConfidenceSignals(surface_attested=True, is_supersede=False)
EXTRACTOR = "emr:deterministic"


async def file_parked_cards(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    parked: list[ParkedRead],
) -> int:
    """File a `low_confidence` card for each OCR read the reconciler parked (§6.4) —
    a readable-but-unmatched reprint is held for review, never minted as a fact. One
    card per (analyte, day) parked read; returns the number filed. Idempotent: a
    re-run re-derives the same (subkind, analyte, collected) key and does not
    duplicate an open card."""
    if not parked:
        return 0
    filed = 0
    async with scoped_session(maker, ctx) as session:
        for p in parked:
            obs = p.observation
            key = f"{obs.analyte.code}|{obs.collected_at.isoformat()}"
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM app.review_items WHERE kind = :k AND status = 'open'"
                        " AND payload->>'subkind' = :sk AND payload->>'note_id' = :nid"
                        " AND payload->>'key' = :key LIMIT 1"
                    ),
                    {"k": REVIEW_KIND, "sk": p.subkind, "nid": str(note_id), "key": key},
                )
            ).first()
            if exists is not None:
                continue
            session.add(
                ReviewItem(
                    kind=REVIEW_KIND,
                    payload={
                        "note_id": str(note_id),
                        "subkind": p.subkind,
                        "key": key,
                        "analyte": obs.analyte.name,
                        "collected": obs.collected_at.isoformat(),
                        "source": obs.source_system,
                        "reason": p.reason,
                    },
                    domain_code=note_domain,
                )
            )
            filed += 1
    return filed


async def file_firewall_cards(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    catches: Sequence[tuple[str, FirewallCatch]],
) -> int:
    """File a `low_confidence` (`subkind=firewall_address`) card per Layer-2 firewall
    catch (§3.6) — the owner's only evidence that the guard fired and held a
    whereabouts fact out of the health graph. Each catch is paired with the id of the
    attachment it was caught in; returns the number filed.

    The card carries WHAT was held and WHERE (attachment, page anchor, page chunk) and
    never the caught value: the value was kept out of the health domain on purpose, and
    this card sits in that same domain, so parking the value in its payload would
    re-plant the leak. It advertises no accept either — a facility address that is
    genuinely wanted is added deliberately as a location-domain `Place` sidecar (§3.6),
    never from here — so `dismiss` is the only verb it offers.

    Deduped on (attachment, anchor, entity kind, predicate) across ALL statuses, so a
    dismissed card never nags again on a re-import. The attachment id keys it rather
    than the chunk id because a re-ingest re-mints chunk rows; the payload still cites
    the chunk for provenance.
    """
    if not catches:
        return 0
    filed = 0
    seen: set[str] = set()
    async with scoped_session(maker, ctx) as session:
        for attachment_id, catch in catches:
            key = f"{attachment_id}|{catch.anchor}|{catch.entity_kind}|{catch.predicate}"
            if key in seen:  # two catches of the same shape on one page are one card
                continue
            seen.add(key)
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM app.review_items WHERE kind = :k"
                        " AND payload->>'subkind' = :sk AND payload->>'note_id' = :nid"
                        " AND payload->>'key' = :key LIMIT 1"
                    ),
                    {
                        "k": FIREWALL_REVIEW_KIND,
                        "sk": catch.subkind,
                        "nid": str(note_id),
                        "key": key,
                    },
                )
            ).first()
            if exists is not None:
                continue
            session.add(
                ReviewItem(
                    kind=FIREWALL_REVIEW_KIND,
                    payload={
                        "note_id": str(note_id),
                        "subkind": catch.subkind,
                        "key": key,
                        "attachment_id": attachment_id,
                        "anchor": catch.anchor,
                        "chunk_id": catch.chunk_id or None,
                        "predicate": catch.predicate,
                        "entity_kind": catch.entity_kind,
                        "summary": (
                            f"location firewall: a {catch.predicate} fact on a health"
                            f" {catch.entity_kind} was held out of the graph"
                        ),
                        "rationale": (
                            f"caught at {catch.anchor}; the value is deliberately not"
                            " recorded here. A facility address that is genuinely needed"
                            " is added as a location-domain Place, never as a health fact."
                        ),
                    },
                    domain_code=note_domain,
                )
            )
            filed += 1
    return filed


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
    RLS-scoped transaction (§6.6). Returns the Layer-2 firewall catches (facts held
    out of the graph) — the caller MUST card them with `file_firewall_cards`, or a
    security control fires silently. Provider resolution runs under `ctx` — pass a
    health-only scope so a general-domain namesake is invisible and a health
    `Person` is minted, not re-matched (§3.6)."""
    # The one LLM touch on the structured path (§6.5): the pathology Final Diagnosis.
    # Fail-soft — an unrouted task / unusable reply yields no diagnoses and the
    # deterministic labs + encounters still commit (the narrative stays searchable).
    diagnoses = await extract_pathology_diagnoses(
        pipeline._router, result.pathology_narrative or ""
    )
    intents, catches = lower_parse_result(
        result, str(note_id), chunk_for_anchor, pathology_diagnoses=diagnoses
    )
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
