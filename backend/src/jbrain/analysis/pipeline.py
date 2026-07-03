"""The integrate_note job handler: one note.extract call -> Integrator -> facts,
entities, mentions, temporal tokens, review items, note_analysis (docs/reference/ANALYSIS.md).

Failure contract: transient LLM faults propagate and ride the queue's normal
retry backoff; an extraction that stayed malformed through the adapter's
re-ask is a PermanentJobError. All writes happen in one transaction, so a
failed run never partial-writes facts, and re-analysis is idempotent: facts
upsert on the structural identity key, mentions rebuild wholesale (the chunks
pattern), tokens are reused by (phrase, resolved value).

A note is captured in one domain, but a fact may ratchet UP (a health reading
in a `general` note). Its citation must not point at a chunk the fact's own RLS
scope cannot see, so `_citation_chunk` derives a per-domain copy of the cited
chunk in the fact's domain — a citation never crosses the firewall
(docs/reference/ANALYSIS.md "Mixed-domain notes").
"""

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import and_, bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis import flow_trace, purge
from jbrain.analysis.appointment_projection import project_appointments
from jbrain.analysis.arbiter import (
    ArbiterPlan,
    compute_signals,
    derive_kinship_gender,
    plan_intent,
    plan_to_extraction,
    recover_dropped_fields,
)
from jbrain.analysis.canonical import (
    PromotionOutcome,
    promote_if_corroborated,
    reproject_canonical_name,
)
from jbrain.analysis.device_binding import reconcile_device_bindings
from jbrain.analysis.display import (
    ambiguous_display,
    collision_display,
    confirm_entity_display,
    inference_display,
    mark_snippet,
    merge_display,
    promotion_display,
    truncation_display,
    value_label,
)
from jbrain.analysis.emr_projection import project_emr
from jbrain.analysis.entities import (
    DISAMBIGUATE_MAX_TOKENS,
    DISAMBIGUATE_SCHEMA,
    DISAMBIGUATE_STRENGTH,
    DISAMBIGUATE_SYSTEM,
    DISAMBIGUATE_TASK,
    AmbiguousEntity,
    NeedsDisambiguation,
    ResolvedEntity,
    alias_owner,
    are_distinct,
    build_disambiguation_prompt,
    create_provisional,
    declared_alias,
    get_or_create_me,
    near_duplicate_entity,
    parse_disambiguation,
    plan_merge,
    register_declared_alias,
    resolve_entity,
)
from jbrain.analysis.extraction import (
    ExtractedFact,
    Extraction,
    ExtractionError,
    domain_floor,
    merge_extractions,
    normalize_future_assertion,
    normalize_past_assertion,
    parse_extraction,
    ratchet_domain,
    recover_scalar_value,
)
from jbrain.analysis.geofence_projection import project_place_geofences
from jbrain.analysis.graph_context import build_graph_context
from jbrain.analysis.integrate import Integrator
from jbrain.analysis.integrate_prompt import INTEGRATE_STRENGTH
from jbrain.analysis.intent import EntityResolution, IntegrationIntent
from jbrain.analysis.persist import IntegrationRunLog
from jbrain.analysis.predicates import alias_canonicals, decide_predicates
from jbrain.analysis.prompt import (
    EXTRACT_MAX_TOKENS,
    EXTRACTION_SCHEMA,
    NOTE_EXTRACT_STRENGTH,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
    fact_cap,
    group_texts,
    prompt_block,
)
from jbrain.analysis.supersession import (
    Candidate,
    Decision,
    FactView,
    decide,
    inverse_predicate,
    is_functional,
)
from jbrain.analysis.trace import build_trace
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient
from jbrain.ingest.chunker import PARAGRAPH
from jbrain.llm import LlmBadResponseError, LlmError, LlmRouter
from jbrain.models.analysis import (
    Entity,
    EntityMention,
    Fact,
    NoteAnalysis,
    ReviewItem,
    TemporalToken,
)
from jbrain.models.notes import Attachment, AttachmentExtract, Chunk, Note
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.schema import SchemaError, get_registry
from jbrain.schema.models import _norm_key
from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger()

_DB_LINK_METHODS = frozenset({"exact_alias", "embedding", "llm", "human"})

# Below embedding auto-link confidence on purpose: a cheap-model verdict over
# near-tie candidates is real evidence, not certainty.
LLM_LINK_CONFIDENCE = 0.8

# Only FULL declared names seed a near-duplicate merge PROPOSAL. A given/family
# component, a preferred name, a nickname, or a bare `name` (pet decomposition)
# is short and low-signal — proposing merges on those resurrects the
# bare-first-name fan-out ANALYSIS rejected (docs/reference/ANALYSIS.md "Same-name
# coexistence"). Canonical spellings only; parse-time normalization already ran.
_NEAR_DUP_PREDICATES = frozenset({"name.full", "name.maiden", "name.aka"})

# Trace fallback when a fact has no computed signal (compute_signals keys every
# fact, so this only guards a future caller): the most cautious reading.
_CONSERVATIVE_SIGNALS = ConfidenceSignals(surface_attested=False, is_supersede=True)


def local_anchor(captured_at: datetime, tz_offset_minutes: int | None) -> datetime:
    """The capture anchor in the note's LOCAL time.

    created_at round-trips through timestamptz as a UTC instant, so on its own
    it tells the model the wrong calendar day (an evening capture serializes as
    the next UTC day). When the client recorded its offset we re-project the
    instant into that offset, so "today"/"in 3 months" resolve against the
    note's local date (docs/reference/ANALYSIS.md "Temporal model"). Offset absent (older
    rows, server-stamped captures): fall back to the instant as stored.
    """
    if tz_offset_minutes is None:
        return captured_at
    return captured_at.astimezone(timezone(timedelta(minutes=tz_offset_minutes)))


@dataclass(frozen=True)
class _ChunkRef:
    id: uuid.UUID
    text: str


# (chunk_id, char_start, char_end) — what _locate anchors a surface to.
_Span = tuple[uuid.UUID, int, int]


def _cite(anchor: _Span | None, chunks: list[_ChunkRef]) -> str | None:
    """The frozen citation a review card shows: the anchoring chunk's snippet
    with the surface span <mark>ed; unmarked head text when nothing anchors."""
    if anchor is None:
        return mark_snippet(chunks[0].text) if chunks else None
    text = next((c.text for c in chunks if c.id == anchor[0]), None)
    return mark_snippet(text, anchor[1], anchor[2])


def _locate(surface: str, chunks: list[_ChunkRef]) -> _Span | None:
    """Span-anchor a surface string: first chunk containing it (exact, then
    case-insensitive); a paraphrased surface anchors to the first chunk with
    a zero-width span rather than being dropped — merges stay reversible."""
    if not chunks:
        return None
    for chunk in chunks:
        idx = chunk.text.find(surface)
        if idx != -1:
            return chunk.id, idx, idx + len(surface)
    lowered = surface.casefold()
    for chunk in chunks:
        idx = chunk.text.casefold().find(lowered)
        if idx != -1:
            return chunk.id, idx, idx + len(surface)
    return chunks[0].id, 0, 0


# The schema version stamped on an IntegrationIntent's provenance. Versioned
# fact-level stamping + the agent-curated schema land in Wave 2; until then the
# registry is at v1 (schema/defs/_meta.yaml).
_SCHEMA_VERSION = 1


def _review_card_domain(predicate: str, note_domain: str) -> str:
    """The domain a review card rides: a sensitive predicate floors a general note
    into its restricted domain, then the ratchet applies — so a card never lands
    in a less-restricted scope than its predicate (the firewall, shared by every
    card filer)."""
    floor = domain_floor(predicate)
    extracted = floor if (floor is not None and note_domain == "general") else note_domain
    card_domain, _ = ratchet_domain(extracted, note_domain)
    return card_domain


async def _extract_note(
    router: LlmRouter,
    texts: list[str],
    *,
    domain: str,
    prompt_anchor: datetime,
    parse_anchor: datetime | None,
    note_id: str,
) -> Extraction:
    """Run the note.extract call(s) over a note's chunk groups and merge them into
    one Extraction, the shared front half of integrate_note so the extraction
    logic lives in one place. Raises
    PermanentJobError if the output is unusable after the adapter's one re-ask —
    retrying would just re-bill the same garbage; a SchemaError is config drift,
    also permanent. Nothing is written here (the merge is in-memory)."""
    try:
        parts: list[Extraction] = []
        for group in group_texts(texts):
            group_cap = fact_cap("\n\n".join(group))
            result = await router.complete(
                "note.extract",
                system=SYSTEM_PROMPT,
                user_text=build_user_prompt(
                    group, anchor=prompt_anchor, domain=domain, max_facts=group_cap
                ),
                json_schema=EXTRACTION_SCHEMA,
                max_tokens=EXTRACT_MAX_TOKENS,
                strength=NOTE_EXTRACT_STRENGTH,
            )
            parts.append(parse_extraction(result.parsed, anchor=parse_anchor, max_facts=group_cap))
        return merge_extractions(parts)
    except (LlmBadResponseError, ExtractionError, SchemaError) as exc:
        raise PermanentJobError(f"note.extract unusable for note {note_id}: {exc}") from exc


class AnalysisPipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        router: LlmRouter,
        *,
        embedder: EmbedClient | None = None,
        embed_model: str = "",
        settings: SqlSettingsStore | None = None,
    ):
        self._maker = maker
        self._router = router
        # The note→graph judgment agent (docs/archive/INTEGRATOR_PLAN.md Track B).
        self._integrator = Integrator(router)
        # Net-new integration run + resolution-pin persistence (§E7b), gated by the
        # integration_persist setting below — inert without a settings store.
        self._runlog = IntegrationRunLog(maker)
        # Optional on purpose: without an embed client, resolution layer 2 is
        # skipped entirely (no degraded guessing) — the harness and older
        # call sites keep their exact behavior.
        self._embedder = embedder
        self._embed_model = embed_model
        # Reads the value_shape_enforce toggle and the predicate_canonicalization
        # toggle (which now gates only the held-fact predicate-suggestion
        # picker); None ⇒ both off, so the harness/older call sites are
        # byte-unchanged.
        self._settings = settings

    async def integrate_note(self, payload: dict[str, Any]) -> None:
        """The note→graph path (docs/archive/INTEGRATOR_PLAN.md): extract → Integrator
        (graph-aware agent judgment) → plan_intent (deterministic disposition) →
        apply_intent (deterministic commit + review cards). Missing/deleted note
        is a no-op."""
        note_id = str(payload["note_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            note = (
                await session.execute(select(Note).where(Note.id == note_id))
            ).scalar_one_or_none()
            if note is None or note.deleted_at is not None:
                log.info("integration.skipped", note_id=note_id, reason="missing or deleted")
                return
            body, domain, captured_at = note.body, note.domain_code, note.created_at
            tz_offset = note.tz_offset_minutes
            # An owner correction note (Phase 6 §4) extracts at full weight and
            # force-supersedes + pins the current head, so it out-argues the graph.
            correction = note.provenance == "owner_correction"
            # The LEFT JOIN pulls the source extract's confidence for a machine-read
            # chunk (matched on attachment + kind, one row per pair). It feeds the
            # transcript marker's "low-confidence" qualifier; NULL for note text.
            chunk_rows = (
                await session.execute(
                    select(
                        Chunk.id,
                        Chunk.text,
                        Chunk.source_kind,
                        Attachment.filename,
                        AttachmentExtract.confidence,
                    )
                    .join(Attachment, Chunk.attachment_id == Attachment.id, isouter=True)
                    .join(
                        AttachmentExtract,
                        and_(
                            AttachmentExtract.attachment_id == Chunk.attachment_id,
                            AttachmentExtract.kind == Chunk.source_kind,
                        ),
                        isouter=True,
                    )
                    .where(Chunk.note_id == note_id, Chunk.granularity == PARAGRAPH)
                    .order_by(Chunk.seq)
                )
            ).all()
        chunks = [_ChunkRef(id=r.id, text=r.text) for r in chunk_rows]
        texts = [
            prompt_block(
                r.text, source_kind=r.source_kind, filename=r.filename, confidence=r.confidence
            )
            for r in chunk_rows
        ] or [body]

        prompt_anchor = local_anchor(captured_at, tz_offset)
        parse_anchor = prompt_anchor if tz_offset is not None else None
        extraction = await _extract_note(
            self._router,
            texts,
            domain=domain,
            prompt_anchor=prompt_anchor,
            parse_anchor=parse_anchor,
            note_id=note_id,
        )
        flow_trace.extract(note_id, extraction)

        # Graph-aware context: the existing entities + active facts near this
        # note's mentions, so the agent can resolve to known entities and propose
        # merges/supersessions instead of always minting new. Runs under the
        # all-seeing SYSTEM_CTX; build_graph_context applies the domain firewall
        # itself (RLS does not scope SYSTEM_CTX). get_or_create_me anchors the
        # owner the agent resolves first person to.
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            owner = await get_or_create_me(session)
            graph_context = await build_graph_context(
                session,
                owner_id=owner.id,
                mentions=extraction.mentions,
                note_domain=domain,
                embedder=self._embedder,
                embed_model=self._embed_model,
            )
        note_text = "\n\n".join(c.text for c in chunks) or body
        intent = await self._integrator.integrate(
            note_id=note_id,
            extraction=extraction,
            graph_context=graph_context,
            schema_version=_SCHEMA_VERSION,
            note_text=note_text,
        )
        flow_trace.intent(note_id, "integrate", intent)
        # Restore objects the integrator dropped when re-typing relationship facts
        # (it non-deterministically omits object_entity_ref the extraction carried),
        # so the edge links instead of orphaning + holding for review.
        intent = recover_dropped_fields(intent, extraction)
        flow_trace.intent(note_id, "recover", intent)
        # Deterministically emit the gender a kinship edge implies for its object
        # (four "daughters" → four female children) when the model captured the
        # edges but omitted gender; _gender_grounded then attests it so it commits.
        intent = derive_kinship_gender(intent, note_text)
        # Collapse durably-aliased predicates BEFORE the arbiter keys facts, so
        # a past owner map/rename decision lands on the canonical graph address;
        # unaliased long-tail predicates commit raw (two-tier model).
        await self.canonicalize_intent(intent)
        signals = compute_signals(intent, [c.text for c in chunks])
        plan = plan_intent(intent, signals, correction=correction)
        flow_trace.plan(note_id, plan, signals)

        provider, model = await self._router.effective_spec("integrate.note", INTEGRATE_STRENGTH)
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            resolved = await self.apply_intent(
                session,
                note_id=uuid.UUID(note_id),
                note_domain=domain,
                captured_at=captured_at,
                chunks=chunks,
                intent=intent,
                plan=plan,
                title=extraction.title,
                tags=extraction.tags,
                extractor=f"{provider}:{model}",
                dropped_facts=extraction.dropped_facts,
            )
            await session.execute(
                update(Note)
                .where(Note.id == uuid.UUID(note_id))
                .values(integration_state="integrated")
            )
        # Net-new run + pin persistence (§E7b), gated. Skipped on a rejected plan:
        # apply_intent committed NOTHING (returns {}), so there is no new decision
        # to record and — critically — re-touching the pin table here would wipe a
        # previously-converged note's pins on a transient rejection (a silent flip,
        # N10). A persistence fault is swallowed: the graph + integration_state are
        # already durable above, so a run-log/pin write must never fail the job (and
        # never roll back the commit — persist runs in its own transaction).
        # SYSTEM_CTX with ran_as='system' recorded on the run: the integration
        # pipeline legitimately crosses every firewall (E1), and the audit says so.
        if (
            not plan.rejected
            and self._settings is not None
            and await self._settings.integration_persist(SYSTEM_CTX)
        ):
            try:
                run_id = await self._runlog.persist(
                    SYSTEM_CTX,
                    note_id=note_id,
                    note_domain=domain,
                    intent=intent,
                    plan=plan,
                    chunks=chunks,
                    resolved=resolved,
                )
                log.info("integration.run_persisted", note_id=note_id, run_id=run_id)
            except Exception as exc:  # noqa: BLE001 — persistence is best-effort audit
                log.warning("integration.persist_failed", note_id=note_id, error=repr(exc))
        log.info(
            "integration.done",
            note_id=note_id,
            committed=len(plan.to_commit),
            review=len(plan.to_review),
        )

    async def apply_intent(
        self,
        session: AsyncSession,
        *,
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
        intent: IntegrationIntent,
        plan: ArbiterPlan,
        title: str,
        tags: list[str],
        extractor: str,
        dropped_facts: int = 0,
    ) -> dict[str, ResolvedEntity | None]:
        """Commit an arbiter-approved IntegrationIntent through the existing
        deterministic _apply (plan §9, Option 1). A rejected plan is a no-op: the
        note stays pending_integration, nothing is written (N5: no partial
        commit). Active-eligible facts commit; review-held facts (cross-subject,
        ambiguous, low weight) are written as inert `pending_review` rows and each
        linked to its low_confidence_inference card — all in this one transaction
        (N5), so a human can later accept (pin) or reject (retract) it.

        Returns the committed mention_ref -> entity map (`{}` for a rejected plan)
        so the caller can persist the Integrator's resolution pins from the SAME
        entities the commit used, without re-resolving (which would double-mint
        provisionals).

        `dropped_facts` is the upstream per-note cap's tail-drop count, carried so
        the rebuilt extraction can file the `extraction_truncated` card (W0). The
        DB-mode eval runner threads the real `extraction.dropped_facts` (it runs
        the cap), matching production; pre-built-plan callers with no extraction
        leave it 0 — no cap ran, so no truncation card is owed."""
        if plan.rejected:
            log.info(
                "integration.rejected",
                note_id=str(note_id),
                violations=[v.code for v in plan.fatal_violations],
            )
            return {}
        override = await self._resolve_from_intent(
            session, list(intent.entity_resolutions), note_domain=note_domain
        )
        # All facts (commit_only=False); held ones are routed to the pending_review
        # path by INDEX — extraction.facts[i] is 1:1 with plan.facts[i], so the key
        # is exact even when two facts share entity_ref.predicate.qualifier (e.g.
        # enumerated children edges).
        extraction = plan_to_extraction(
            intent, plan, title=title, tags=tags, dropped_facts=dropped_facts
        )
        held_indices = frozenset(
            i for i, pf in enumerate(plan.facts) if pf.status == "pending_review"
        )
        held_ids = await self._apply(
            session,
            note_id=note_id,
            note_domain=note_domain,
            captured_at=captured_at,
            chunks=chunks,
            extraction=extraction,
            extractor=extractor,
            resolution_override=override,
            held_indices=held_indices,
        )
        # Recompute the deterministic signals (pure, cheap) so each held card can
        # carry the same ceiling arithmetic the arbiter used — apply_intent is also
        # called standalone (eval harness) with a pre-built plan, so the signals
        # aren't threaded in.
        signals = compute_signals(intent, [c.text for c in chunks])
        await self._file_inference_reviews(
            session,
            note_id=note_id,
            note_domain=note_domain,
            intent=intent,
            plan=plan,
            signals=signals,
            held_ids=held_ids,
        )
        return override

    async def _resolve_from_intent(
        self,
        session: AsyncSession,
        resolutions: list[EntityResolution],
        *,
        note_domain: str,
    ) -> dict[str, ResolvedEntity | None]:
        """Validate the agent's coreference into a name(=mention_ref)→entity
        override (plan §9). An existing-mode ref is honored only if its entity is
        fetchable under the session's scope; missing/out-of-scope/malformed-id →
        None (the fact then skips — never a guess, and a synthetic ref can't be
        re-resolved). new-mode mints a provisional; ambiguous → None."""
        override: dict[str, ResolvedEntity | None] = {}
        for r in resolutions:
            if r.mode == "existing" and r.proposed_entity_id:
                try:
                    eid = uuid.UUID(r.proposed_entity_id)
                except ValueError:
                    override[r.mention_ref] = None
                    continue
                entity = (
                    await session.execute(select(Entity).where(Entity.id == eid))
                ).scalar_one_or_none()
                override[r.mention_ref] = (
                    ResolvedEntity(
                        id=entity.id, subject_id=entity.subject_id, created=False, method="llm"
                    )
                    if entity is not None
                    else None
                )
            elif r.mode == "new" and r.new_kind and r.new_name:
                override[r.mention_ref] = await create_provisional(
                    session, r.new_name, kind_hint=r.new_kind, domain=note_domain
                )
            else:
                override[r.mention_ref] = None
        return override

    async def _file_inference_reviews(
        self,
        session: AsyncSession,
        *,
        note_id: uuid.UUID,
        note_domain: str,
        intent: IntegrationIntent,
        plan: ArbiterPlan,
        signals: dict[int, ConfidenceSignals],
        held_ids: dict[int, uuid.UUID],
    ) -> None:
        """Surface every review-held fact (cross-subject, ambiguous, or
        below-threshold) the arbiter would not auto-commit as a
        low_confidence_inference card, so it is owner-visible rather than dropped
        (plan N11, A1b-ii-2). The held fact is also written as a pending_review
        row (_insert_held_fact); `held_ids[i]` is that row's id (by plan.facts
        index), carried in the payload as `fact_id` so accept pins it / reject
        retracts it. The card's domain rides the same floor/ratchet the fact does,
        so a sensitive inference never lands in a less-restricted scope than its
        predicate."""
        # Per-fact provenance the process trace reads: how each mention resolved,
        # and what supersession the agent proposed for this exact key.
        resolutions = {r.mention_ref: r for r in intent.entity_resolutions}
        supersessions = {
            (s.entity_ref, s.predicate, s.qualifier): s.action
            for s in intent.supersession_proposals
        }
        registry = get_registry()
        # Weighted relation candidates for the correct-in-place predicate picker:
        # the canonicals nearest each held predicate, by embedding similarity, so
        # the card offers a ranked list to swap the relation onto. One embed call
        # for all held facts. Inert without an embedder or with the
        # canonicalization setting off — the picker then offers manual entry only.
        pred_suggestions: dict[int, list[dict[str, Any]]] = {}
        if (
            self._embedder is not None
            and self._settings is not None
            and await self._settings.predicate_canonicalization(SYSTEM_CTX)
        ):
            held = [
                (i, pf.fact) for i, pf in enumerate(plan.facts) if pf.status == "pending_review"
            ]
            if held:
                ranked = await decide_predicates(
                    session,
                    [(f.predicate, f.statement, f.kind) for _, f in held],
                    embedder=self._embedder,
                )
                for (i, _), suggestions in zip(held, ranked, strict=True):
                    pred_suggestions[i] = [{"name": n, "score": s} for n, s in suggestions]
        for i, pf in enumerate(plan.facts):
            if pf.status != "pending_review":
                continue
            fact = pf.fact
            # Mirror _upsert_fact's domain derivation. An IntentFact carries no
            # domain (plan_to_extraction emits ExtractedFact.domain=""), so
            # _upsert_fact's `fact.domain or note_domain` collapses to note_domain
            # — we start there, then apply the same floor + ratchet.
            card_domain = _review_card_domain(fact.predicate, note_domain)
            # Re-analysis must not multiply identical open cards.
            existing = (
                await session.execute(
                    text(
                        "SELECT 1 FROM app.review_items"
                        " WHERE kind = 'low_confidence_inference' AND status = 'open'"
                        " AND payload->>'note_id' = :nid AND payload->>'entity_ref' = :ref"
                        " AND payload->>'predicate' = :pred AND payload->>'qualifier' = :qual"
                        " LIMIT 1"
                    ),
                    {
                        "nid": str(note_id),
                        "ref": fact.entity_ref,
                        "pred": fact.predicate,
                        "qual": fact.qualifier,
                    },
                )
            ).first()
            if existing is not None:
                continue
            held_id = held_ids.get(i)
            # Render the card from the COMMITTED held fact (the row the note view
            # reads), not the raw planned fact: _insert_held_fact shape-checked and
            # coerced its value_json ("Female (inferred from 'wife')" -> "female"),
            # so reading it back here keeps the card and the note in lockstep — one
            # source of truth, no snapshot drift between the two surfaces.
            card_value_json = fact.value_json
            card_statement = fact.statement
            if held_id is not None:
                row = (
                    await session.execute(
                        select(Fact.value_json, Fact.statement).where(Fact.id == held_id)
                    )
                ).first()
                if row is not None:
                    card_value_json, card_statement = row.value_json, row.statement
            # A typed (closed-enum) predicate carries its members so the card can
            # offer a pick-a-member correction instead of free text — gender →
            # {male, female, unknown}. Empty (and so omitted) for free-text edges.
            enum_members = registry.enum_values_for(fact.predicate)
            session.add(
                ReviewItem(
                    kind="low_confidence_inference",
                    payload={
                        "note_id": str(note_id),
                        "entity_ref": fact.entity_ref,
                        "predicate": fact.predicate,
                        "qualifier": fact.qualifier,
                        "fact_kind": fact.kind,
                        # The fact's modality, so the card can offer a
                        # correct-in-place picker (asserted/negated/hypothetical/
                        # …). A correction flips it via the prose-note channel.
                        "assertion": fact.assertion,
                        "statement": card_statement,
                        # The structured value the card renders as `predicate →
                        # value`, so the owner sees the exact fact they're
                        # approving — not only the prose statement.
                        "value_json": card_value_json,
                        **({"enum_values": list(enum_members)} if enum_members else {}),
                        "weight": pf.weight,
                        "reasons": list(pf.review_reasons),
                        "title": card_statement,
                        # fact_id links the card to the pending_review row it
                        # represents — accept pins it, reject retracts it. None
                        # only if the held fact couldn't be written (unresolved
                        # entity); the card still surfaces it.
                        "fact_id": str(held_id) if held_id is not None else None,
                        # The verbose extraction -> integration -> arbiter trace the
                        # review UI plays back (optional dropdown), so a held fact is
                        # debuggable from the card without re-reading logs.
                        "trace": build_trace(
                            fact,
                            pf,
                            signals.get(i, _CONSERVATIVE_SIGNALS),
                            resolution=resolutions.get(fact.entity_ref),
                            supersession_action=supersessions.get(
                                (fact.entity_ref, fact.predicate, fact.qualifier)
                            ),
                            extract_version=PROMPT_VERSION,
                            integrate_version=intent.prompt_version,
                            integrator_version=intent.integrator_version,
                        ),
                        # The ranked relation candidates the predicate picker
                        # offers (omitted when there's no embedder to weight them).
                        **(
                            {"predicate_suggestions": pred_suggestions[i]}
                            if pred_suggestions.get(i)
                            else {}
                        ),
                        **inference_display(
                            statement=card_statement,
                            reasons=list(pf.review_reasons),
                            snippet=None,
                        ),
                    },
                    domain_code=card_domain,
                )
            )

    async def canonicalize_intent(self, intent: IntegrationIntent) -> None:
        """Public entry for the durable predicate-alias collapse — the supported
        seam the eval harness calls (production integrate_note uses it too)."""
        await self._canonicalize_predicates(intent)

    async def _canonicalize_predicates(self, intent: IntegrationIntent) -> None:
        """Collapse each unknown predicate in the intent through the durable
        `predicate_aliases` map (past owner map/rename decisions) before the
        arbiter keys it. An unaliased predicate is tier-2 long-tail: it commits
        raw — no embed round-trip, no new_predicate card, never rejected
        (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md §1)."""
        registry = get_registry()
        unknown = [
            (i, f)
            for i, f in enumerate(intent.facts)
            if not registry.declares_predicate(f.predicate)
        ]
        if not unknown:
            return
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            aliases = await alias_canonicals(session, [f.predicate for _, f in unknown])
        kept: set[str] = set()  # one longtail log line per raw spelling per run
        for i, fact in unknown:
            canonical = aliases.get(_norm_key(fact.predicate))
            if canonical is not None:
                intent.facts[i] = replace(fact, predicate=canonical)
                self._rewrite_supersession(intent, fact.predicate, canonical)
                log.info("predicate.canonicalized", raw=fact.predicate, canonical=canonical)
            elif fact.predicate not in kept:
                kept.add(fact.predicate)
                log.info(
                    "predicate.longtail_kept",
                    predicate=fact.predicate,
                    kind=fact.kind,
                    note_id=intent.note_id,
                )

    @staticmethod
    def _rewrite_supersession(intent: IntegrationIntent, raw: str, canonical: str) -> None:
        """Carry a STRONG predicate rewrite into the matching supersession
        proposals, so compute_signals keys is_supersede on the SAME (canonical)
        predicate the rewritten fact now uses — otherwise the proposal would name
        the raw predicate and the supersession would silently drop."""
        for j, sp in enumerate(intent.supersession_proposals):
            if sp.predicate == raw:
                intent.supersession_proposals[j] = replace(sp, predicate=canonical)

    async def _apply(
        self,
        session: AsyncSession,
        *,
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
        extraction: Extraction,
        extractor: str,
        resolution_override: dict[str, ResolvedEntity | None] | None = None,
        held_indices: frozenset[int] = frozenset(),
    ) -> dict[int, uuid.UUID]:
        """Write a note's extraction. Facts whose index is in `held_indices` are
        written as inert `pending_review` rows (the arbiter held them); the rest
        go through the normal commit path. Returns {index: fact_id} for the held
        rows so the caller can link each to its review card. The default empty set
        commits every fact through the normal path."""
        resolved = await self._resolve_entities(
            session, extraction, note_id, note_domain, chunks, captured_at, resolution_override
        )
        anchor_for = await self._rebuild_mentions(
            session, extraction, resolved, note_id, note_domain, chunks
        )
        token_ids = await self._upsert_tokens(
            session, extraction, note_id, note_domain, captured_at, chunks
        )

        touched: set[uuid.UUID] = set()
        held_ids: dict[int, uuid.UUID] = {}
        for i, fact in enumerate(extraction.facts):
            if i in held_indices:
                fact_id = await self._insert_held_fact(
                    session,
                    fact=fact,
                    resolved=resolved,
                    token_ids=token_ids,
                    anchor_for=anchor_for,
                    note_id=note_id,
                    note_domain=note_domain,
                    captured_at=captured_at,
                    chunks=chunks,
                    extractor=extractor,
                )
                if fact_id is not None:
                    held_ids[i] = fact_id
            else:
                fact_id = await self._upsert_fact(
                    session,
                    fact=fact,
                    resolved=resolved,
                    token_ids=token_ids,
                    anchor_for=anchor_for,
                    note_id=note_id,
                    note_domain=note_domain,
                    captured_at=captured_at,
                    chunks=chunks,
                    extractor=extractor,
                )
            # Both paths' ids enter `touched` so the sweep below never retracts a
            # fact this run still asserts — including a still-held pending_review
            # row (without this, re-analysis would churn its id and orphan the
            # open card's fact_id link).
            if fact_id is not None:
                touched.add(fact_id)
            await session.flush()

        await self._register_declared_aliases(
            session, extraction, resolved, note_id, note_domain, chunks
        )

        # Identity keys this note no longer asserts were removed by the edit:
        # retract quietly — not a conflict, no inbox noise. Pinned facts are
        # human decisions and survive (docs/reference/ANALYSIS.md "Reprocessing"). Derived
        # shadows are excluded: their lifecycle mirrors their source's, not the
        # note's re-extraction set, so the source's own refresh/supersession (or
        # FK cascade on its deletion) governs them, never this sweep. RETURNING
        # carries the (unchanged) superseded_by/valid_from of the rows actually
        # retracted — the doomed maps the chain repair below walks.
        sweep = (
            update(Fact)
            .where(
                Fact.note_id == note_id,
                Fact.pinned.is_(False),
                Fact.derived_from_fact_id.is_(None),
                Fact.status.in_(("active", "pending_review")),
            )
            .values(status="retracted")
            .returning(Fact.id, Fact.superseded_by, Fact.valid_from, Fact.entity_id)
        )
        if touched:
            sweep = sweep.where(Fact.id.not_in(touched))
        swept = (await session.execute(sweep)).all()
        # A derived shadow follows its source's fate. The sweep above excludes
        # shadows (it governs only note-sourced facts), so when it retracts a
        # source the re-extraction no longer asserts, close that source's
        # reciprocal in the same breath — otherwise a dropped relationship would
        # leave a stale active inverse on the object's stream.
        shadow_swept = (
            await session.execute(
                update(Fact)
                .where(
                    Fact.derived_from_fact_id.in_(
                        select(Fact.id).where(Fact.note_id == note_id, Fact.status == "retracted")
                    ),
                    Fact.pinned.is_(False),
                    Fact.status.in_(("active", "pending_review")),
                )
                .values(status="retracted")
                .returning(Fact.id, Fact.superseded_by, Fact.valid_from, Fact.entity_id)
            )
        ).all()
        retracted = [*swept, *shadow_swept]
        if retracted:
            # A retracted fact must not keep other facts superseded: survivors
            # re-attach past the doomed links or are restored — the same repair
            # the note-deletion purge runs, including intra-note chains.
            doomed_links = {r.id: r.superseded_by for r in retracted}
            await purge.repair_chains(
                session, doomed_links, {r.id: r.valid_from for r in retracted}
            )
            # Open cards referencing a retracted fact are unservable noise;
            # resolved/dismissed items are human history and pinned facts never
            # entered the doomed set, so both survive untouched.
            await purge.delete_review_items(session, set(doomed_links), statuses=("open",))
        await self._sweep_stale_ambiguous(session, note_id, extraction)
        await self._sync_truncation_review(
            session, note_id, note_domain, chunks, extraction.dropped_facts, len(extraction.facts)
        )
        await self._reproject_entities(session, resolved)
        await self._promote_corroborated(session, resolved)

        stmt = pg_insert(NoteAnalysis).values(
            note_id=note_id,
            title=extraction.title or None,
            tags=extraction.tags,
            extractor=extractor,
            prompt_version=PROMPT_VERSION,
            analyzed_at=datetime.now(UTC),
            domain_code=note_domain,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[NoteAnalysis.note_id],
                set_={
                    "title": stmt.excluded.title,
                    "tags": stmt.excluded.tags,
                    "extractor": stmt.excluded.extractor,
                    "prompt_version": stmt.excluded.prompt_version,
                    "analyzed_at": stmt.excluded.analyzed_at,
                    "domain_code": stmt.excluded.domain_code,
                },
            )
        )

        # Refresh the appointments projection for every entity this note touched —
        # the ones it re-asserted (resolved) and the ones whose facts it retracted
        # (a reschedule lands on the same appointment entity; a dropped mention
        # leaves it with no active scheduledTime, so its row is removed).
        projected = {e.id for e in resolved.values() if e is not None}
        projected.update(r.entity_id for r in retracted)
        await project_appointments(session, projected)
        await project_emr(session, projected)
        await project_place_geofences(session, projected)
        # Bind any touched Device entity to its operational subject row (owner-set,
        # deterministic, never LLM-chosen). Rides the same full-owner fact-apply
        # path as the geofence projection so a device note links on apply.
        await reconcile_device_bindings(session, projected)
        return held_ids

    async def _sweep_stale_ambiguous(
        self, session: AsyncSession, note_id: uuid.UUID, extraction: Extraction
    ) -> None:
        """Retire open ambiguous_mention cards for names the re-extraction no
        longer references — the dedup in _file_ambiguous_review only stops new
        duplicates, it never retires obsolete ones. Open-only: a resolved or
        dismissed card is a human decision and survives any re-run."""
        names = {m.name for m in extraction.mentions}
        for fact in extraction.facts:
            for ref in (fact.entity_ref, fact.object_entity_ref):
                if ref:
                    names.add(ref)
        clause = " AND payload->>'name' NOT IN :names" if names else ""
        stmt = text(
            "DELETE FROM app.review_items"
            " WHERE kind = 'ambiguous_mention' AND status = 'open'"
            " AND payload->>'note_id' = :nid" + clause
        )
        params: dict[str, Any] = {"nid": str(note_id)}
        if names:
            stmt = stmt.bindparams(bindparam("names", expanding=True))
            params["names"] = sorted(names)
        await session.execute(stmt, params)

    async def _sync_truncation_review(
        self,
        session: AsyncSession,
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
        dropped: int,
        kept: int,
    ) -> None:
        """Surface a hit fact-budget as a review card, and clear it once a re-run
        no longer truncates. The cap keeps the model's salient head and drops the
        tail silently (extraction.parse_extraction); for a genuinely long note
        (a pasted article, a medical-history dump) that tail is real signal, so
        the owner gets a dismissible notice with the re-run hint. One open card
        per note: dedup like the ambiguous sweep so re-analysis never stacks
        duplicates, and a larger-budget re-run that fits retires the stale card."""
        if dropped <= 0:
            await session.execute(
                text(
                    "DELETE FROM app.review_items WHERE kind = 'extraction_truncated'"
                    " AND status = 'open' AND payload->>'note_id' = :nid"
                ),
                {"nid": str(note_id)},
            )
            return
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM app.review_items WHERE kind = 'extraction_truncated'"
                    " AND status = 'open' AND payload->>'note_id' = :nid LIMIT 1"
                ),
                {"nid": str(note_id)},
            )
        ).first()
        payload = {
            "note_id": str(note_id),
            **truncation_display(kept=kept, dropped=dropped, snippet=_cite(None, chunks)),
        }
        if existing is not None:
            # Refresh the counts in place — a re-run may clip a different amount —
            # without churning the row's identity or its open status.
            await session.execute(
                update(ReviewItem).where(ReviewItem.id == existing.id).values(payload=payload)
            )
            return
        session.add(
            ReviewItem(kind="extraction_truncated", payload=payload, domain_code=note_domain)
        )

    async def _resolve_entities(
        self,
        session: AsyncSession,
        extraction: Extraction,
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
        captured_at: datetime,
        resolution_override: dict[str, ResolvedEntity | None] | None = None,
    ) -> dict[str, ResolvedEntity | None]:
        """Layered resolution for every name the extraction references
        (docs/reference/ANALYSIS.md "Alias resolution & separation"): exact alias, the
        relationship hop for reference-shaped mentions at the note's capture
        time, embedding similarity, then one batched entity.disambiguate call
        for whatever is still undecided.

        Ambiguous names resolve to None (no link) and file one deduplicated
        ambiguous_mention review item.
        """
        kind_hints = {m.name: m.kind for m in extraction.mentions}
        # A fact-only reference has no mention surface; the name itself is
        # the best span to cite for it.
        surfaces = {m.name: m.surface_text for m in reversed(extraction.mentions)}
        names: list[str] = []
        for mention in extraction.mentions:
            if mention.name not in names:
                names.append(mention.name)
        for fact in extraction.facts:
            for ref in (fact.entity_ref, fact.object_entity_ref):
                if ref and ref not in names:
                    names.append(ref)

        resolved: dict[str, ResolvedEntity | None] = {}
        pending: dict[str, NeedsDisambiguation] = {}
        for name in names:
            if resolution_override is not None and name in resolution_override:
                # The Integrator agent already resolved this mention; honor its
                # validated choice instead of re-resolving (plan §9, Option 1).
                # Synthetic mention_ref "names" can't be re-resolved anyway, and
                # a non-rejected plan covers every fact ref, so this branch is
                # taken for all of them.
                resolved[name] = resolution_override[name]
                continue
            outcome = await resolve_entity(
                session,
                name,
                kind_hint=kind_hints.get(name, "Thing"),
                domain=note_domain,
                note_time=captured_at,
                # Models normalize reference mentions to invented names; the
                # verbatim surface keeps the shape the resolver hops on.
                surface=surfaces.get(name),
                embedder=self._embedder,
                embed_model=self._embed_model,
            )
            if isinstance(outcome, NeedsDisambiguation):
                pending[name] = outcome
            elif isinstance(outcome, AmbiguousEntity):
                resolved[name] = None
                await self._file_ambiguous_review(
                    session,
                    name,
                    note_id,
                    note_domain,
                    outcome.candidate_ids,
                    snippet=_cite(_locate(surfaces.get(name, name), chunks), chunks),
                )
            else:
                resolved[name] = outcome
        resolved.update(
            await self._disambiguate(
                session,
                pending,
                note_id=note_id,
                note_domain=note_domain,
                kind_hints=kind_hints,
                surfaces=surfaces,
                chunks=chunks,
            )
        )
        return resolved

    async def _disambiguate(
        self,
        session: AsyncSession,
        pending: dict[str, NeedsDisambiguation],
        *,
        note_id: uuid.UUID,
        note_domain: str,
        kind_hints: dict[str, str],
        surfaces: dict[str, str],
        chunks: list[_ChunkRef],
    ) -> dict[str, ResolvedEntity | None]:
        """Layer 3: ONE batched cheap call for the note's undecided mentions —
        conditional, never per-mention (docs/reference/ANALYSIS.md "Model routing &
        cost"). Every failure mode — task not routed (the harness router only
        carries note.extract), bad JSON after the adapter's re-ask, an
        unanswered mention, a hallucinated id — degrades to the review inbox:
        an uncertain resolver files a card, it never guesses. A "none of
        these" verdict is an answer, not a failure: the mention is a
        genuinely new entity.
        """
        if not pending:
            return {}
        choices: dict[str, str | None] = {}
        answered = False
        try:
            self._router.spec(DISAMBIGUATE_TASK)
        except LlmError:
            log.info("analysis.disambiguate_unrouted", note_id=str(note_id))
        else:
            items = [
                {
                    "name": name,
                    "kind": kind_hints.get(name, "Thing"),
                    "context": _cite(_locate(surfaces.get(name, name), chunks), chunks),
                    "candidates": [
                        {"id": str(c.id), "name": c.name, "kind": c.kind, "summary": c.summary}
                        for c in need.candidates
                    ],
                }
                for name, need in pending.items()
            ]
            try:
                result = await self._router.complete(
                    DISAMBIGUATE_TASK,
                    system=DISAMBIGUATE_SYSTEM,
                    user_text=build_disambiguation_prompt(items),
                    json_schema=DISAMBIGUATE_SCHEMA,
                    max_tokens=DISAMBIGUATE_MAX_TOKENS,
                    strength=DISAMBIGUATE_STRENGTH,
                )
                choices = parse_disambiguation(result.parsed)
                answered = True
            except (LlmError, LlmBadResponseError) as exc:
                # Resolution uncertainty is not extraction failure: the note's
                # facts still land wherever resolution did succeed.
                log.warning("analysis.disambiguate_failed", note_id=str(note_id), error=repr(exc))

        out: dict[str, ResolvedEntity | None] = {}
        for name, need in pending.items():
            by_id = {str(c.id): c for c in need.candidates}
            if answered and name in choices:
                chosen = choices[name]
                if chosen is None:
                    out[name] = await create_provisional(
                        session, name, kind_hint=kind_hints.get(name, "Thing"), domain=note_domain
                    )
                    continue
                candidate = by_id.get(chosen)
                if candidate is not None:
                    out[name] = ResolvedEntity(
                        id=candidate.id,
                        subject_id=candidate.subject_id,
                        method="llm",
                        confidence=LLM_LINK_CONFIDENCE,
                    )
                    continue
            out[name] = None
            await self._file_ambiguous_review(
                session,
                name,
                note_id,
                note_domain,
                sorted(c.id for c in need.candidates),
                snippet=_cite(_locate(surfaces.get(name, name), chunks), chunks),
            )
        return out

    async def _file_ambiguous_review(
        self,
        session: AsyncSession,
        name: str,
        note_id: uuid.UUID,
        note_domain: str,
        candidate_ids: list[uuid.UUID],
        *,
        snippet: str | None,
    ) -> None:
        # Re-analysis must not multiply identical open items.
        existing = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.review_items"
                    " WHERE kind = 'ambiguous_mention' AND status = 'open'"
                    " AND payload->>'name' = :name AND payload->>'note_id' = :nid LIMIT 1"
                ),
                {"name": name, "nid": str(note_id)},
            )
        ).first()
        if existing is not None:
            return
        session.add(
            ReviewItem(
                kind="ambiguous_mention",
                payload={
                    "name": name,
                    "note_id": str(note_id),
                    "entity_ids": [str(c) for c in candidate_ids],
                    **ambiguous_display(name=name, snippet=snippet),
                },
                domain_code=note_domain,
            )
        )

    async def _rebuild_mentions(
        self,
        session: AsyncSession,
        extraction: Extraction,
        resolved: dict[str, ResolvedEntity | None],
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
    ) -> dict[str, _Span]:
        """Delete + insert this note's mentions (the chunks pattern from
        ingest); returns name -> anchoring span for fact provenance and the
        <mark>ed citations review items carry."""
        await session.execute(delete(EntityMention).where(EntityMention.note_id == note_id))
        anchor_for: dict[str, _Span] = {}
        for mention in extraction.mentions:
            entity = resolved.get(mention.name)
            located = _locate(mention.surface_text, chunks)
            if located is not None and mention.name not in anchor_for:
                anchor_for[mention.name] = located
            if entity is None or located is None:
                continue
            chunk_id, start, end = located
            session.add(
                EntityMention(
                    entity_id=entity.id,
                    chunk_id=chunk_id,
                    note_id=note_id,
                    surface_text=mention.surface_text,
                    char_start=start,
                    char_end=end,
                    # 0006 CHECKs link_method to exact_alias|embedding|llm|
                    # human; the deterministic relationship hop rides
                    # exact_alias (it is rule-based linking too) until a
                    # migration widens the enum.
                    link_method=(
                        entity.method if entity.method in _DB_LINK_METHODS else "exact_alias"
                    ),
                    confidence=entity.confidence,
                    domain_code=note_domain,
                )
            )
        return anchor_for

    async def _reproject_entities(
        self, session: AsyncSession, resolved: dict[str, ResolvedEntity | None]
    ) -> None:
        """Once this note's facts have settled, refresh each touched entity's
        canonical_name from its current name.* facts — a projection of current
        facts (docs/reference/ANALYSIS.md), never the frozen first-mention surface form."""
        seen: set[uuid.UUID] = set()
        for entity in resolved.values():
            if entity is None or entity.id in seen:
                continue
            seen.add(entity.id)
            await reproject_canonical_name(session, entity.id)

    async def _promote_corroborated(
        self, session: AsyncSession, resolved: dict[str, ResolvedEntity | None]
    ) -> None:
        """Confirm each touched provisional entity that >= CORROBORATION_THRESHOLD
        distinct same-domain notes now corroborate (docs/reference/entity.md). Eager and
        complete: an entity only crosses the bar on a note that references it, and
        that note's refs are exactly `resolved`, so no sweep is needed. A
        contested identity (a live namesake) files a deduped confirm_entity card
        instead of auto-confirming. Gated by the entity_promotion setting
        (default off until the goldens expect confirmation)."""
        if self._settings is None or not await self._settings.entity_promotion(SYSTEM_CTX):
            return
        seen: set[uuid.UUID] = set()
        for entity in resolved.values():
            if entity is None or entity.id in seen:
                continue
            seen.add(entity.id)
            outcome = await promote_if_corroborated(session, entity.id)
            if outcome.action == "confirmed":
                log.info("entity.promoted", entity_id=str(entity.id))
            elif outcome.action == "propose":
                await self._file_confirm_entity_card(session, outcome)

    async def _file_confirm_entity_card(
        self, session: AsyncSession, outcome: "PromotionOutcome"
    ) -> None:
        """File a confirm_entity card for a corroborated-but-contested entity,
        deduped on entity_id across ALL statuses so a dismissed proposal never
        nags again on re-analysis."""
        exists = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.review_items WHERE kind = 'confirm_entity'"
                    " AND payload->>'entity_id' = :id LIMIT 1"
                ),
                {"id": str(outcome.entity_id)},
            )
        ).first()
        if exists is not None:
            return
        session.add(
            ReviewItem(
                kind="confirm_entity",
                payload={
                    "entity_id": str(outcome.entity_id),
                    "entity_name": outcome.name,
                    "entity_kind": outcome.kind,
                    **confirm_entity_display(name=outcome.name, kind=outcome.kind),
                },
                domain_code=outcome.domain,
            )
        )

    async def _register_declared_aliases(
        self,
        session: AsyncSession,
        extraction: Extraction,
        resolved: dict[str, ResolvedEntity | None],
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
    ) -> None:
        """A self-naming fact ("my full name is Jeffrey Mark Hopkins") teaches
        the resolver an exact alias, so a later bare "Jeffrey Mark Hopkins"
        lands on Me instead of forking a new entity. ASSERTED facts only — a
        reported, negated, hypothetical, or questioned name is not a
        declaration. When the declared name already keys a DIFFERENT entity the
        alias is NOT widened across both (the wrong silent link); that collision
        is instead surfaced as a merge_proposal — the one high-confidence
        same-person signal worth auto-suggesting (docs/reference/ANALYSIS.md "Alias
        resolution & separation")."""
        for fact in extraction.facts:
            if fact.assertion != "asserted":
                continue
            name = declared_alias(fact.predicate, fact.value_json)
            entity = resolved.get(fact.entity_ref) if name is not None else None
            if name is None or entity is None:
                continue
            added = await register_declared_alias(session, entity.id, name)
            if added is not None:
                log.info("analysis.alias_declared", entity_id=str(entity.id), alias=added)
                # A FULL declared name may be a near (not exact) duplicate of a
                # different entity — the same-person signal the exact collision
                # below cannot see. Propose, never link. Restricted to full-name
                # predicates so a first name / nickname / pet name never fans a
                # merge card out across same-named people (docs/reference/ANALYSIS.md
                # "Same-name coexistence").
                if fact.predicate in _NEAR_DUP_PREDICATES:
                    await self._propose_near_duplicate(
                        session, entity, name, note_id, note_domain, chunks
                    )
                continue
            other = await alias_owner(session, name, exclude=entity.id)
            if other is not None:
                await self._propose_merge(
                    session, entity.id, other, name, note_id, note_domain, chunks
                )

    async def _propose_near_duplicate(
        self,
        session: AsyncSession,
        entity: ResolvedEntity,
        name: str,
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
    ) -> None:
        """File a merge proposal when a freshly declared full name strongly
        embeds to a DIFFERENT same-kind entity. Embedder-gated (skipped without
        it, so the harness is unaffected) and proposal-only — `near_duplicate_entity`
        drops cross-subject candidates, and `_propose_merge` honours the
        distinct_from edge and dedupes across re-analysis."""
        if self._embedder is None:
            return
        kind = await session.scalar(
            text("SELECT kind FROM app.entities WHERE id = :id"), {"id": str(entity.id)}
        )
        dup = await near_duplicate_entity(
            session,
            name,
            kind_hint=kind or "",
            domain=note_domain,
            embedder=self._embedder,
            embed_model=self._embed_model,
            exclude=entity.id,
            exclude_subject=entity.subject_id,
        )
        if dup is not None:
            await self._propose_merge(session, entity.id, dup, name, note_id, note_domain, chunks)

    async def _propose_merge(
        self,
        session: AsyncSession,
        a: uuid.UUID,
        b: uuid.UUID,
        name: str,
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
    ) -> None:
        """File a merge_proposal for two entities a self-naming fact tied to the
        same name. Honours the permanent distinct_from edge (a rejected merge is
        never re-proposed) and dedupes open cards across re-analysis. Direction
        comes from plan_merge so the owner / confirmed side always survives."""
        if await are_distinct(session, a, b):
            return
        plan = await plan_merge(session, a, b)
        # entity_a is the survivor, entity_b the tombstoned side — the merge
        # handler reads exactly this direction. Dedup is direction-agnostic: one
        # open card per unordered pair, however the next note phrases it.
        keep, gone = str(plan.keep_id), str(plan.gone_id)
        existing = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.review_items WHERE kind = 'merge_proposal'"
                    " AND status = 'open' AND payload->>'entity_a' IN (:x, :y)"
                    " AND payload->>'entity_b' IN (:x, :y) LIMIT 1"
                ),
                {"x": keep, "y": gone},
            )
        ).first()
        if existing is not None:
            return
        snippet = _cite(_locate(name, chunks), chunks)
        session.add(
            ReviewItem(
                kind="merge_proposal",
                payload={
                    "entity_a": keep,
                    "entity_b": gone,
                    "note_id": str(note_id),
                    **merge_display(
                        keep_name=plan.keep_name, gone_name=plan.gone_name, snippet=snippet
                    ),
                },
                domain_code=note_domain,
            )
        )
        log.info("analysis.merge_proposed", keep=str(plan.keep_id), gone=str(plan.gone_id))

    async def _upsert_tokens(
        self,
        session: AsyncSession,
        extraction: Extraction,
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
    ) -> dict[tuple[str, str], uuid.UUID]:
        """Get-or-create temporal tokens keyed on (phrase, resolved start).

        Tokens are never deleted while facts may cite them (0006 grants), so
        re-analysis reuses rows instead of rebuilding wholesale.
        """
        existing = (
            await session.execute(select(TemporalToken).where(TemporalToken.note_id == note_id))
        ).scalars()
        token_ids: dict[tuple[str, str], uuid.UUID] = {
            (t.surface_phrase, t.resolved_start.isoformat()): t.id for t in existing
        }
        for token in extraction.tokens:
            key = (token.phrase, token.resolved_start.isoformat())
            if key in token_ids:
                continue
            located = _locate(token.phrase, chunks)
            row = TemporalToken(
                id=uuid.uuid4(),
                note_id=note_id,
                chunk_id=located[0] if located else None,
                surface_phrase=token.phrase,
                kind=token.kind,
                resolved_start=token.resolved_start,
                resolved_end=token.resolved_end,
                temporal_precision=token.precision,
                capture_anchor=captured_at,
                rrule=token.rrule,
                domain_code=note_domain,
            )
            session.add(row)
            token_ids[key] = row.id
        await session.flush()
        return token_ids

    async def _token_for_fact(
        self,
        session: AsyncSession,
        phrase: str,
        start: datetime,
        end: datetime | None,
        precision: str,
        token_ids: dict[tuple[str, str], uuid.UUID],
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
    ) -> uuid.UUID:
        key = (phrase, start.isoformat())
        if key in token_ids:
            return token_ids[key]
        located = _locate(phrase, chunks)
        row = TemporalToken(
            id=uuid.uuid4(),
            note_id=note_id,
            chunk_id=located[0] if located else None,
            surface_phrase=phrase,
            kind="point" if end is None else "range",
            resolved_start=start,
            resolved_end=end,
            temporal_precision=precision,
            capture_anchor=captured_at,
            domain_code=note_domain,
        )
        session.add(row)
        await session.flush()
        token_ids[key] = row.id
        return row.id

    async def _existing_facts(
        self,
        session: AsyncSession,
        entity_id: uuid.UUID,
        predicate: str,
        qualifier: str,
        subject_id: uuid.UUID | None,
        object_entity_id: uuid.UUID | None,
        fact_domain: str,
    ) -> list[FactView]:
        # Candidate retrieval is scoped to same entity+DOMAIN(+kind, handled by
        # decide). The structural identity key is the graph ADDRESS
        # entity.predicate[.qualifier] pointing at a value or another entity, so
        # an edge to a different object is a DIFFERENT fact (me.owns->Civic vs
        # me.owns->kayak) while a scalar fact has a null object. Functional
        # predicates are the exception: at most one current value across ALL
        # objects (a new employer must see — and supersede — the old employer
        # edge), so the object stays out of their key. The pipeline runs as the
        # owner SYSTEM_CTX, so RLS does not scope this read — without the
        # explicit domain filter a health fact would supersede a same-key
        # general fact and a review card would copy cross-domain text
        # (docs/reference/ANALYSIS.md "Domains and the firewall", "Facts").
        stmt = select(Fact).where(
            Fact.entity_id == entity_id,
            Fact.predicate == predicate,
            Fact.qualifier == qualifier,
            Fact.subject_id == subject_id if subject_id else Fact.subject_id.is_(None),
            Fact.domain_code == fact_domain,
        )
        if not is_functional(predicate):
            stmt = stmt.where(
                Fact.object_entity_id == object_entity_id
                if object_entity_id
                else Fact.object_entity_id.is_(None)
            )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            FactView(
                id=str(f.id),
                kind=f.kind,
                statement=f.statement,
                value_json=f.value_json,
                object_entity_id=str(f.object_entity_id) if f.object_entity_id else None,
                assertion=f.assertion,
                valid_from=f.valid_from,
                valid_to=f.valid_to,
                reported_at=f.reported_at,
                status=f.status,
                pinned=f.pinned,
                # NULL (pre-column rows) reads as confident: a low-confidence
                # candidate must not displace a row of unknown confidence.
                confidence=f.confidence if f.confidence is not None else 1.0,
                derived=f.derived_from_fact_id is not None,
            )
            for f in rows
        ]

    async def _citation_chunk(
        self,
        session: AsyncSession,
        *,
        source_chunk_id: uuid.UUID | None,
        fact_domain: str,
        note_domain: str,
        note_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """The chunk a fact in `fact_domain` may cite without crossing the
        firewall. A note's chunks all carry its capture domain, so a ratcheted
        fact (a health reading in a `general` note) would otherwise cite a chunk
        its own RLS scope cannot see. Derive a get-or-create `derived` copy of
        the source chunk in the fact's domain and cite that instead — the
        citation never leaves the fact's scope (docs/reference/ANALYSIS.md "Mixed-domain
        notes"). A same-domain fact cites the source chunk directly."""
        if source_chunk_id is None or fact_domain == note_domain:
            return source_chunk_id
        src = str(source_chunk_id)
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM app.chunks WHERE note_id = :n AND domain_code = :d"
                    " AND source_kind = 'derived' AND source_anchor = :src LIMIT 1"
                ),
                {"n": str(note_id), "d": fact_domain, "src": src},
            )
        ).first()
        if existing is not None:
            return existing.id
        new_id = uuid.uuid4()
        # Copy the span verbatim (same char offsets, same text) so the stored
        # fact anchor still marks the right snippet; only the domain changes.
        # No embedding: derived chunks are citation backing, not search rows.
        await session.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq,"
                " char_start, char_end, source_kind, source_anchor, text)"
                " SELECT :new, note_id, :d, granularity, seq, char_start, char_end,"
                " 'derived', :anchor, text FROM app.chunks WHERE id = :src_id"
            ),
            {"new": str(new_id), "d": fact_domain, "anchor": src, "src_id": src},
        )
        return new_id

    async def _insert_held_fact(
        self,
        session: AsyncSession,
        *,
        fact: ExtractedFact,
        resolved: dict[str, ResolvedEntity | None],
        token_ids: dict[tuple[str, str], uuid.UUID],
        anchor_for: dict[str, _Span],
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
        extractor: str,
    ) -> uuid.UUID | None:
        """Write an arbiter-held fact (cross-subject / ambiguous / below-threshold)
        as an inert `pending_review` row. Deliberately NOT through decide(): a held
        fact must never supersede, activate, or materialize an inverse — that is the
        review guarantee (N3). It still gets the deterministic domain floor/ratchet
        (firewall) and a citation chunk. Idempotent on re-analysis: this note's
        existing pending_review row for the same identity key is refreshed in place,
        so the id (and the open card's fact_id link) survives. Returns the row id so
        _apply adds it to `touched` and the card can reference it; None when the
        entity (or object) didn't resolve, exactly like _upsert_fact."""
        fact = normalize_past_assertion(normalize_future_assertion(fact, captured_at), captured_at)
        entity = resolved.get(fact.entity_ref)
        if entity is None:
            log.info("analysis.held_fact_skipped", reason="unlinked entity", ref=fact.entity_ref)
            return None
        object_entity: ResolvedEntity | None = None
        if fact.object_entity_ref:
            object_entity = resolved.get(fact.object_entity_ref)
            if object_entity is None:
                log.info(
                    "analysis.held_fact_skipped",
                    reason="unlinked object",
                    ref=fact.object_entity_ref,
                )
                return None

        extracted_domain = fact.domain or note_domain
        floor = domain_floor(fact.predicate)
        if floor is not None and extracted_domain == "general":
            extracted_domain = floor
        # A held fact is not promoting anything yet, so the ratchet's promotion
        # flag is intentionally ignored (no domain_promotion card for a held row).
        fact_domain, _ = ratchet_domain(extracted_domain, note_domain)

        valid_from = valid_to = None
        precision = "unknown"
        token_id: uuid.UUID | None = None
        if fact.temporal is not None:
            valid_from = fact.temporal.resolved_start
            valid_to = fact.temporal.resolved_end
            precision = fact.temporal.precision
            if fact.temporal.phrase and valid_from is not None:
                token_id = await self._token_for_fact(
                    session,
                    fact.temporal.phrase,
                    valid_from,
                    valid_to,
                    precision,
                    token_ids,
                    note_id,
                    note_domain,
                    captured_at,
                    chunks,
                )

        object_id = object_entity.id if object_entity else None
        # Shape-check once up front so both the in-place refresh and the fresh
        # held row commit the same (possibly value-dropped) payload.
        fact = replace(
            fact,
            value_json=await self._shape_check(
                session,
                entity_id=entity.id,
                predicate=fact.predicate,
                statement=fact.statement,
                value_json=fact.value_json,
                object_present=object_id is not None,
            ),
        )
        # Idempotency: refresh this note's existing held row for the identity key
        # rather than churn a fresh id (which the sweep would then orphan the card
        # off of). decide() is bypassed, so this lookup stands in for its refresh.
        obj_clause = (
            Fact.object_entity_id == object_id
            if object_id is not None
            else Fact.object_entity_id.is_(None)
        )
        existing_id = (
            (
                await session.execute(
                    select(Fact.id).where(
                        Fact.note_id == note_id,
                        Fact.entity_id == entity.id,
                        Fact.predicate == fact.predicate,
                        Fact.qualifier == fact.qualifier,
                        obj_clause,
                        Fact.domain_code == fact_domain,
                        Fact.status == "pending_review",
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_id is not None:
            await session.execute(
                update(Fact)
                .where(Fact.id == existing_id)
                .values(
                    statement=fact.statement,
                    value_json=fact.value_json,
                    assertion=fact.assertion,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    temporal_precision=precision,
                    temporal_token_id=token_id,
                    confidence=fact.confidence,
                    extractor=extractor,
                    prompt_version=PROMPT_VERSION,
                )
            )
            return existing_id

        anchor = anchor_for.get(fact.entity_ref)
        base_chunk = anchor[0] if anchor else (chunks[0].id if chunks else None)
        chunk_id = await self._citation_chunk(
            session,
            source_chunk_id=base_chunk,
            fact_domain=fact_domain,
            note_domain=note_domain,
            note_id=note_id,
        )
        held = Fact(
            id=uuid.uuid4(),
            subject_id=entity.subject_id,
            entity_id=entity.id,
            predicate=fact.predicate,
            qualifier=fact.qualifier,
            kind=fact.kind,
            statement=fact.statement,
            value_json=fact.value_json,
            object_entity_id=object_id,
            assertion=fact.assertion,
            valid_from=valid_from,
            valid_to=valid_to,
            reported_at=captured_at,
            temporal_precision=precision,
            temporal_token_id=token_id,
            status="pending_review",
            note_id=note_id,
            chunk_id=chunk_id,
            extractor=extractor,
            prompt_version=PROMPT_VERSION,
            confidence=fact.confidence,
            domain_code=fact_domain,
        )
        session.add(held)
        await session.flush()
        return held.id

    async def _shape_check(
        self,
        session: AsyncSession,
        *,
        entity_id: uuid.UUID,
        predicate: str,
        statement: str,
        value_json: dict[str, Any] | None,
        object_present: bool,
    ) -> dict[str, Any] | None:
        """Typed value-shape validation (Phase 1/4, docs/reference/PREDICATE_CANONICALIZATION.md).
        Returns the value_json to commit: when the model left value_json null it is
        first deterministically recovered from the statement where it can be (an
        enum member, a name.* lead-in), so the page shows the value, not the whole
        sentence; an enum value the model wrote as prose is coerced to its declared
        member ("Female (inferred from 'wife')" -> "female"); the result is then
        returned unchanged when it fits the predicate's declared shape, or None
        (dropped — the fact survives on its statement, the storage invariant) when
        it violates the shape AND the value_shape_enforce setting is on. Default is
        log-only (returns it unchanged); enforcement is flipped live after the eval
        confirms no false drops. Kind is per entity-type, so this runs here (entity
        resolved) not at parse time."""
        registry = get_registry()
        # Drift/unknown predicates no type declares have no shape to validate and
        # nothing to recover against — never rejected.
        if not registry.declares_predicate(predicate):
            return value_json
        kind = (
            await session.execute(select(Entity.kind).where(Entity.id == entity_id))
        ).scalar_one_or_none()
        if kind is None:
            return value_json
        pred = registry.predicate_for_kind(kind, predicate)
        if pred is None:
            return value_json
        # Recover a concise value the model omitted (value_json null, prose only):
        # an enum member or a name after its lead-in. Leaves it null when nothing
        # recovers, so the fact still falls back to its statement (no worse).
        if value_json is None:
            enum_values = pred.enum_values if pred.value_shape == "enum" else ()
            value_json = recover_scalar_value(predicate, statement, enum_values)
            if value_json is None:
                return None
        # Normalize an enum value the model wrote as prose ("Female (inferred from
        # 'wife')") down to its member ("female") BEFORE validating, so the stored
        # datum — and the review card that renders it — reads as the clean member.
        value_json = registry.coerce_value(pred, value_json)
        if registry.validate_value(pred, value_json, object_present=object_present):
            return value_json
        enforce = self._settings is not None and await self._settings.value_shape_enforce(
            SYSTEM_CTX
        )
        event = "fact_value_shape_dropped" if enforce else "fact_value_shape_mismatch"
        log.warning(f"analysis.{event}", predicate=predicate, shape=pred.value_shape, kind=kind)
        return None if enforce else value_json

    async def _upsert_fact(
        self,
        session: AsyncSession,
        *,
        fact: ExtractedFact,
        resolved: dict[str, ResolvedEntity | None],
        token_ids: dict[tuple[str, str], uuid.UUID],
        anchor_for: dict[str, _Span],
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
        extractor: str,
    ) -> uuid.UUID | None:
        # A still-future fact is `expected`, never an asserted past event; and an
        # undated "used to" relationship is CLOSED, not current — both resolved
        # against the note's capture anchor (docs/reference/ANALYSIS.md "Temporal model").
        fact = normalize_past_assertion(normalize_future_assertion(fact, captured_at), captured_at)
        entity = resolved.get(fact.entity_ref)
        if entity is None:
            log.info("analysis.fact_skipped", reason="unlinked entity", ref=fact.entity_ref)
            return None
        object_entity: ResolvedEntity | None = None
        if fact.object_entity_ref:
            object_entity = resolved.get(fact.object_entity_ref)
            if object_entity is None:
                log.info(
                    "analysis.fact_skipped",
                    reason="unlinked object",
                    ref=fact.object_entity_ref,
                )
                return None

        # Deterministic floor first: a clearly-sensitive predicate raises a
        # general/unclassified fact into its restricted domain (firewall
        # hardening), then the asymmetric ratchet applies as usual.
        extracted_domain = fact.domain or note_domain
        floor = domain_floor(fact.predicate)
        if floor is not None and extracted_domain == "general":
            extracted_domain = floor
        fact_domain, needs_promotion = ratchet_domain(extracted_domain, note_domain)

        valid_from = valid_to = None
        precision = "unknown"
        token_id: uuid.UUID | None = None
        if fact.temporal is not None:
            valid_from = fact.temporal.resolved_start
            valid_to = fact.temporal.resolved_end
            precision = fact.temporal.precision
            if fact.temporal.phrase and valid_from is not None:
                token_id = await self._token_for_fact(
                    session,
                    fact.temporal.phrase,
                    valid_from,
                    valid_to,
                    precision,
                    token_ids,
                    note_id,
                    note_domain,
                    captured_at,
                    chunks,
                )

        # Rebuild the fact with the shape-checked value so every downstream write
        # (the Candidate decide() reads, the fresh insert, an in-place close) uses
        # it — enforcement drops a shape-violating value_json here, once.
        fact = replace(
            fact,
            value_json=await self._shape_check(
                session,
                entity_id=entity.id,
                predicate=fact.predicate,
                statement=fact.statement,
                value_json=fact.value_json,
                object_present=object_entity is not None,
            ),
        )
        candidate = Candidate(
            kind=fact.kind,
            statement=fact.statement,
            value_json=fact.value_json,
            object_entity_id=str(object_entity.id) if object_entity else None,
            assertion=fact.assertion,
            valid_from=valid_from,
            valid_to=valid_to,
            reported_at=captured_at,
            confidence=fact.confidence,
            self_confidence=fact.self_confidence,
            correction=fact.correction,
            fhir_status=fact.fhir_status,
        )
        existing = await self._existing_facts(
            session,
            entity.id,
            fact.predicate,
            fact.qualifier,
            entity.subject_id,
            object_entity.id if object_entity else None,
            fact_domain,
        )
        decision = decide(candidate, existing, predicate=fact.predicate)
        flow_trace.commit(
            str(note_id),
            entity_ref=fact.entity_ref,
            predicate=fact.predicate,
            qualifier=fact.qualifier,
            object_ref=fact.object_entity_ref,
            subject_id=entity.id,
            object_id=object_entity.id if object_entity else None,
            existing=existing,
            decision=decision,
        )

        if decision.close_id is not None:
            # In-place interval close: the candidate is the END of the existing
            # open state, not a new value — one row, no chain link, no review.
            # value_json/statement are rewritten too: the closing note's
            # rendering ("...until March") carries the end-marker the open
            # row's payload lacks, and the scenario-facing value must show it.
            fact_id = uuid.UUID(decision.close_id)
            await session.execute(
                update(Fact)
                .where(Fact.id == fact_id)
                .values(
                    statement=fact.statement,
                    value_json=fact.value_json,
                    valid_to=decision.close_valid_to,
                    extractor=extractor,
                    prompt_version=PROMPT_VERSION,
                    confidence=fact.confidence,
                )
            )
            # A derived shadow's lifecycle mirrors its source's: copy the
            # interval close (and re-render) so the reciprocal closes too.
            await self._update_shadows_in_place(
                session, source_id=fact_id, valid_to=decision.close_valid_to
            )
            return fact_id

        if decision.refresh_id is not None:
            # Same identity key, same value: refresh the rendering and provenance in
            # place — citations survive, no chain link, no duplicate row.
            fact_id = uuid.UUID(decision.refresh_id)
            values: dict[str, Any] = {
                "statement": fact.statement,
                "extractor": extractor,
                "prompt_version": PROMPT_VERSION,
                "confidence": fact.confidence,
            }
            refreshed = next((e for e in existing if e.id == decision.refresh_id), None)
            # Re-analysis healing: a row still held purely by WEIGHT (it carries an
            # open low_confidence_inference card) that the arbiter now rates active is
            # PROMOTED in place — editing a note / fixing the pipeline and
            # re-analyzing is how an under-attested fact should clear review, rather
            # than staying stuck behind a stale flag. Its card is then unservable, and
            # an open relationship edge gets the reciprocal the held row never minted.
            # A row held for a STRUCTURAL reason (a fact_conflict contradiction, an
            # attribute_collision) must NOT be promoted: that conflict is unresolved,
            # and this refresh path runs BEFORE decide()'s conflict branch on
            # idempotent re-ingest, so it would otherwise silently flip a parked
            # negation live. Gate strictly on the weight-hold card. Idempotent: once
            # active there is no held row (and no card) to promote on later runs.
            promoted = False
            if refreshed is not None and refreshed.status == "pending_review":
                promoted = (
                    await session.execute(
                        select(ReviewItem.id)
                        .where(
                            ReviewItem.kind == "low_confidence_inference",
                            ReviewItem.status == "open",
                            ReviewItem.payload["fact_id"].astext == str(fact_id),
                        )
                        .limit(1)
                    )
                ).first() is not None
            if promoted:
                values["status"] = "active"
            # This note DIRECTLY asserts what so far was only a derived shadow of
            # another note's edge: adopt it as a primary fact owned by THIS note,
            # so the human-stated claim survives deletion of the source that
            # first reflected it (red-team Finding 1). Without this, "Celine's
            # spouse is Jeff" in its own note would silently ride Jeff's note and
            # vanish when his note is deleted.
            if refreshed is not None and refreshed.derived:
                anchor = anchor_for.get(fact.entity_ref)
                base_chunk = anchor[0] if anchor else (chunks[0].id if chunks else None)
                values["derived_from_fact_id"] = None
                values["note_id"] = note_id
                values["chunk_id"] = await self._citation_chunk(
                    session,
                    source_chunk_id=base_chunk,
                    fact_domain=fact_domain,
                    note_domain=note_domain,
                    note_id=note_id,
                )
            await session.execute(update(Fact).where(Fact.id == fact_id).values(values))
            await self._update_shadows_in_place(session, source_id=fact_id)
            if promoted:
                # The row's open review card is now unservable (the fact committed);
                # resolved/dismissed history survives. Then give an open relationship
                # edge the reciprocal a held edge never got (_materialize_inverse
                # dedups, so no duplicate if one already exists).
                await purge.delete_review_items(session, {fact_id}, statuses=("open",))
                if fact.kind == "relationship" and object_entity is not None and valid_to is None:
                    anchor = anchor_for.get(fact.entity_ref)
                    base_chunk = anchor[0] if anchor else (chunks[0].id if chunks else None)
                    await self._materialize_inverse(
                        session,
                        fact=fact,
                        source_fact_id=fact_id,
                        entity=entity,
                        object_entity=object_entity,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        precision=precision,
                        token_id=token_id,
                        note_id=note_id,
                        fact_domain=fact_domain,
                        captured_at=captured_at,
                        chunk_id=await self._citation_chunk(
                            session,
                            source_chunk_id=base_chunk,
                            fact_domain=fact_domain,
                            note_domain=note_domain,
                            note_id=note_id,
                        ),
                        extractor=extractor,
                        snippet=_cite(anchor, chunks),
                    )
            return fact_id

        anchor = anchor_for.get(fact.entity_ref)
        base_chunk = anchor[0] if anchor else (chunks[0].id if chunks else None)
        chunk_id = await self._citation_chunk(
            session,
            source_chunk_id=base_chunk,
            fact_domain=fact_domain,
            note_domain=note_domain,
            note_id=note_id,
        )
        # Explicit id: read below before flush would otherwise be None
        # (the ORM default fires at flush time).
        new_fact = Fact(
            id=uuid.uuid4(),
            subject_id=entity.subject_id,
            entity_id=entity.id,
            predicate=fact.predicate,
            qualifier=fact.qualifier,
            kind=fact.kind,
            statement=fact.statement,
            value_json=fact.value_json,
            object_entity_id=object_entity.id if object_entity else None,
            assertion=fact.assertion,
            valid_from=valid_from,
            valid_to=decision.insert_valid_to or valid_to,
            reported_at=captured_at,
            temporal_precision=precision,
            temporal_token_id=token_id,
            status=decision.insert_status,
            pinned=decision.insert_pinned,
            superseded_by=(
                uuid.UUID(decision.insert_superseded_by) if decision.insert_superseded_by else None
            ),
            note_id=note_id,
            chunk_id=chunk_id,
            extractor=extractor,
            prompt_version=PROMPT_VERSION,
            confidence=fact.confidence,
            domain_code=fact_domain,
        )
        session.add(new_fact)
        await session.flush()
        # The held side of the collision, for the card's "previously
        # recorded" choice label.
        conflict = next((e for e in existing if e.id == decision.conflicting_id), None)
        await self._apply_decision_side_effects(
            session,
            decision,
            new_fact.id,
            fact,
            fact_domain,
            note_id,
            valid_from,
            conflict=conflict,
            snippet=_cite(anchor, chunks),
        )
        # Reciprocity: a directed relationship edge inserted ACTIVE gets its
        # inverse materialized on the object's stream, then the old source's
        # derived shadow re-pointed at it. Order matters — the new inverse must
        # exist before propagation can chain a superseded shadow onto it.
        # A reciprocal is a CURRENT-value computation: only an active AND OPEN
        # edge gets one. A closed (former) relationship — "used to work for X" —
        # must NOT mint "X employs Me", or that derived edge would answer
        # "who works for X?" with the owner, smuggling a past job back as current
        # (docs/archive/research/legacy-links-plan.md §ledger F1).
        new_inverse_id: uuid.UUID | None = None
        if (
            fact.kind == "relationship"
            and object_entity is not None
            and decision.insert_status == "active"
            and (decision.insert_valid_to or valid_to) is None
        ):
            new_inverse_id = await self._materialize_inverse(
                session,
                fact=fact,
                source_fact_id=new_fact.id,
                entity=entity,
                object_entity=object_entity,
                valid_from=valid_from,
                valid_to=decision.insert_valid_to or valid_to,
                precision=precision,
                token_id=token_id,
                note_id=note_id,
                fact_domain=fact_domain,
                captured_at=captured_at,
                chunk_id=chunk_id,
                extractor=extractor,
                snippet=_cite(anchor, chunks),
            )
        if decision.supersede_ids:
            # Chain the old shadow onto the NEW inverse when one exists (a clean
            # mirror of the source chain). When none does — the predicate is
            # unknown, or the cross-subject gate refused to write an inverse —
            # the shadow has no successor on its own entity, so close it with a
            # null link rather than pointing it cross-entity at the source fact
            # (red-team Finding 2).
            await self._propagate_supersession_to_shadows(
                session,
                source_ids=[uuid.UUID(i) for i in decision.supersede_ids],
                successor_id=new_inverse_id,
                valid_from=valid_from,
            )
        if needs_promotion:
            session.add(
                ReviewItem(
                    kind="domain_promotion",
                    payload={
                        "fact_id": str(new_fact.id),
                        "note_id": str(note_id),
                        "note_domain": note_domain,
                        "proposed_domain": fact.domain,
                        **promotion_display(
                            predicate=fact.predicate,
                            proposed=fact.domain,
                            note_domain=note_domain,
                            snippet=_cite(anchor, chunks),
                        ),
                    },
                    domain_code=fact_domain,
                )
            )
        return new_fact.id

    async def _apply_decision_side_effects(
        self,
        session: AsyncSession,
        decision: Decision,
        new_fact_id: uuid.UUID,
        fact: ExtractedFact,
        fact_domain: str,
        note_id: uuid.UUID,
        valid_from: datetime | None,
        *,
        conflict: FactView | None,
        snippet: str | None,
    ) -> None:
        for old_id in decision.supersede_ids:
            values: dict[str, Any] = {"status": "superseded", "superseded_by": new_fact_id}
            if valid_from is not None:
                # SCD-2 close: the old fact stays true about its interval; an
                # interval already closed by better information is kept.
                values["valid_to"] = func.coalesce(Fact.valid_to, valid_from)
            await session.execute(update(Fact).where(Fact.id == uuid.UUID(old_id)).values(values))
        for old_id in decision.hold_ids:
            await session.execute(
                update(Fact).where(Fact.id == uuid.UUID(old_id)).values(status="pending_review")
            )
        if decision.review_kind is not None:
            # Structured fields mirroring the inference card, so a conflict/collision
            # is correctable IN PLACE (predicate + value + modality) and not only by
            # picking fact_a/fact_b verbatim — an edit files a correction note (the #7
            # channel), never a hand-written fact. The editable side is fact_b, the
            # value this note proposes. enum_values rides only for a typed predicate.
            enum_members = get_registry().enum_values_for(fact.predicate)
            session.add(
                ReviewItem(
                    kind=decision.review_kind,
                    payload={
                        "fact_a": decision.conflicting_id,
                        "fact_b": str(new_fact_id),
                        "predicate": fact.predicate,
                        "qualifier": fact.qualifier,
                        "fact_kind": fact.kind,
                        "assertion": fact.assertion,
                        "statement": fact.statement,
                        "value_json": fact.value_json,
                        **({"enum_values": list(enum_members)} if enum_members else {}),
                        "note_id": str(note_id),
                        # The subject the card is about, so the review UI groups
                        # this conflict under its entity instead of the catch-all
                        # "Other" bucket (frontend reads payload.entity_ref).
                        "entity_ref": fact.entity_ref,
                        **collision_display(
                            kind=decision.review_kind,
                            predicate=fact.predicate,
                            entity_ref=fact.entity_ref,
                            changed=bool(decision.supersede_ids),
                            label_a=(
                                value_label(conflict.value_json, conflict.statement)
                                if conflict
                                else "the earlier value"
                            ),
                            label_b=value_label(fact.value_json, fact.statement),
                            snippet=snippet,
                        ),
                        **decision.review_extra,
                    },
                    domain_code=fact_domain,
                )
            )

    async def _materialize_inverse(
        self,
        session: AsyncSession,
        *,
        fact: ExtractedFact,
        source_fact_id: uuid.UUID,
        entity: ResolvedEntity,
        object_entity: ResolvedEntity,
        valid_from: datetime | None,
        valid_to: datetime | None,
        precision: str,
        token_id: uuid.UUID | None,
        note_id: uuid.UUID,
        fact_domain: str,
        captured_at: datetime,
        chunk_id: uuid.UUID | None,
        extractor: str,
        snippet: str | None,
    ) -> uuid.UUID | None:
        """Write the reciprocal of a directed relationship edge on the object's
        stream, marked derived (docs/archive/research/fix-options/2). Returns the new
        inverse fact id, or None when nothing was written (unknown predicate or
        the cross-subject gate fired)."""
        inverse_pred = inverse_predicate(fact.predicate)
        if inverse_pred is None:
            return None  # not a relation we know how to reciprocate — safe default

        # Cross-subject firewall gate: an inverse lands a fact on the OBJECT's
        # stream. If that object is a DISTINCT security subject, auto-writing it
        # would attribute knowledge across a subject boundary — a leak. Propose
        # it to the review inbox and write nothing (docs/archive/research/fix-options/2,
        # "the single most important rule"). Same-subject / null-subject is safe.
        if object_entity.subject_id is not None and object_entity.subject_id != entity.subject_id:
            session.add(
                ReviewItem(
                    kind="inverse_proposal",
                    payload={
                        "source_fact_id": str(source_fact_id),
                        "note_id": str(note_id),
                        "predicate": inverse_pred,
                        "subject": fact.object_entity_ref,
                        "object": fact.entity_ref,
                        "summary": (
                            f"propose {fact.object_entity_ref}'s {inverse_pred} is"
                            f" {fact.entity_ref}"
                        ),
                        "snippet": snippet,
                    },
                    # The derived edge always inherits the SOURCE fact's domain,
                    # so the proposal does too — never the object's domain.
                    domain_code=fact_domain,
                )
            )
            return None

        statement = f"{fact.object_entity_ref}'s {inverse_pred} is {fact.entity_ref}."
        candidate = Candidate(
            kind="relationship",
            statement=statement,
            value_json=None,
            object_entity_id=str(entity.id),
            assertion=fact.assertion,
            valid_from=valid_from,
            valid_to=valid_to,
            reported_at=captured_at,
            confidence=fact.confidence,
            self_confidence=fact.self_confidence,
        )
        existing = await self._existing_facts(
            session,
            object_entity.id,
            inverse_pred,
            fact.qualifier,
            object_entity.subject_id,
            entity.id,
            fact_domain,
        )
        decision = decide(candidate, existing, predicate=inverse_pred)

        # The reciprocal already exists in a compatible form — a note asserting
        # BOTH directions, or a re-mention — so refresh provenance in place
        # rather than inserting a duplicate derived row. close_id (a pure-edge
        # interval close) likewise updates the existing row, never duplicates.
        if decision.refresh_id is not None or decision.close_id is not None:
            existing_id = uuid.UUID(decision.refresh_id or decision.close_id)  # type: ignore[arg-type]
            values: dict[str, Any] = {
                "statement": statement,
                "extractor": extractor,
                "prompt_version": PROMPT_VERSION,
                "confidence": fact.confidence,
            }
            if decision.close_id is not None:
                values["valid_to"] = decision.close_valid_to
            await session.execute(update(Fact).where(Fact.id == existing_id).values(values))
            return existing_id

        # Derived-defers-to-primary: a derived candidate may supersede another
        # DERIVED shadow, but never a PRIMARY head. If decide() would close a
        # primary, drop the supersession and route to fact_conflict instead so
        # a human adjudicates the reflection against the human-sourced claim.
        by_id = {e.id: e for e in existing}
        primaries = [i for i in decision.supersede_ids if not by_id[i].derived]
        if primaries:
            decision = Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=primaries[0],
            )

        new_inverse = Fact(
            id=uuid.uuid4(),
            subject_id=object_entity.subject_id,
            entity_id=object_entity.id,
            predicate=inverse_pred,
            qualifier=fact.qualifier,
            kind="relationship",
            statement=statement,
            value_json=None,
            object_entity_id=entity.id,
            assertion=fact.assertion,
            valid_from=valid_from,
            valid_to=decision.insert_valid_to or valid_to,
            reported_at=captured_at,
            temporal_precision=precision,
            temporal_token_id=token_id,
            status=decision.insert_status,
            superseded_by=(
                uuid.UUID(decision.insert_superseded_by) if decision.insert_superseded_by else None
            ),
            derived_from_fact_id=source_fact_id,
            note_id=note_id,
            chunk_id=chunk_id,
            extractor=extractor,
            prompt_version=PROMPT_VERSION,
            confidence=fact.confidence,
            domain_code=fact_domain,
        )
        session.add(new_inverse)
        await session.flush()

        for old_id in decision.supersede_ids:
            values: dict[str, Any] = {"status": "superseded", "superseded_by": new_inverse.id}
            if valid_from is not None:
                values["valid_to"] = func.coalesce(Fact.valid_to, valid_from)
            await session.execute(update(Fact).where(Fact.id == uuid.UUID(old_id)).values(values))
        if decision.review_kind is not None:
            conflict = by_id.get(decision.conflicting_id) if decision.conflicting_id else None
            session.add(
                ReviewItem(
                    kind=decision.review_kind,
                    payload={
                        "fact_a": decision.conflicting_id,
                        "fact_b": str(new_inverse.id),
                        "predicate": inverse_pred,
                        "note_id": str(note_id),
                        # The inverse edge is about the object entity — group the
                        # card under it (empty ref falls back to "Other").
                        "entity_ref": fact.object_entity_ref or "",
                        # The card copy must flag that one side is the system's
                        # own reflection, so a human isn't asked to adjudicate it
                        # against the primary as if it were an independent claim.
                        "derived": True,
                        **collision_display(
                            kind=decision.review_kind,
                            predicate=inverse_pred,
                            entity_ref=fact.object_entity_ref or "",
                            changed=bool(decision.supersede_ids),
                            label_a=(
                                value_label(conflict.value_json, conflict.statement)
                                if conflict
                                else "the earlier value"
                            ),
                            label_b=value_label(None, statement),
                            snippet=snippet,
                        ),
                    },
                    domain_code=fact_domain,
                )
            )
        return new_inverse.id

    async def _propagate_supersession_to_shadows(
        self,
        session: AsyncSession,
        *,
        source_ids: list[uuid.UUID],
        successor_id: uuid.UUID | None,
        valid_from: datetime | None,
    ) -> None:
        """When a later note supersedes a source edge, its derived shadow must
        close too — status superseded, SCD-2 valid_to, superseded_by pointing at
        the newly-materialized inverse. A None successor (unknown predicate or a
        cross-subject inverse that was only proposed) closes the shadow with a
        null link, so the chain ends cleanly on its own entity rather than
        pointing cross-entity at the source. Keeps a derived chain a faithful
        mirror of its source's chain."""
        values: dict[str, Any] = {"status": "superseded", "superseded_by": successor_id}
        if valid_from is not None:
            values["valid_to"] = func.coalesce(Fact.valid_to, valid_from)
        await session.execute(
            update(Fact)
            .where(Fact.derived_from_fact_id.in_(source_ids), Fact.status != "superseded")
            .values(values)
        )

    async def _update_shadows_in_place(
        self,
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        valid_to: datetime | None = None,
    ) -> None:
        """Mirror a source's in-place refresh/close onto its derived shadow:
        copy the valid_to (close) so the reciprocal interval ends with its
        source's. The shadow's statement is display-only and already renders
        the relationship, so only the temporal bound needs copying."""
        if valid_to is None:
            return
        await session.execute(
            update(Fact).where(Fact.derived_from_fact_id == source_id).values(valid_to=valid_to)
        )
