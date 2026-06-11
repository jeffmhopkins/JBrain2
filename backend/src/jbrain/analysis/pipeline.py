"""The analyze_note job handler: one note.extract call -> facts, entities,
mentions, temporal tokens, review items, note_analysis (docs/ANALYSIS.md).

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
(docs/ANALYSIS.md "Mixed-domain notes").
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis import purge
from jbrain.analysis.canonical import reproject_canonical_name
from jbrain.analysis.display import (
    ambiguous_display,
    collision_display,
    mark_snippet,
    merge_display,
    promotion_display,
    value_label,
)
from jbrain.analysis.entities import (
    DISAMBIGUATE_MAX_TOKENS,
    DISAMBIGUATE_SCHEMA,
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
    normalize_future_assertion,
    parse_extraction,
    ratchet_domain,
)
from jbrain.analysis.prompt import (
    EXTRACT_MAX_TOKENS,
    EXTRACTION_SCHEMA,
    NOTE_EXTRACT_STRENGTH,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
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
from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient
from jbrain.llm import LlmBadResponseError, LlmError, LlmRouter
from jbrain.models.analysis import (
    EntityMention,
    Fact,
    NoteAnalysis,
    ReviewItem,
    TemporalToken,
)
from jbrain.models.notes import Attachment, Chunk, Note
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.schema import SchemaError

log = structlog.get_logger()

_DB_LINK_METHODS = frozenset({"exact_alias", "embedding", "llm", "human"})

# Below embedding auto-link confidence on purpose: a cheap-model verdict over
# near-tie candidates is real evidence, not certainty.
LLM_LINK_CONFIDENCE = 0.8

# Only FULL declared names seed a near-duplicate merge PROPOSAL. A given/family
# component, a preferred name, a nickname, or a bare `name` (pet decomposition)
# is short and low-signal — proposing merges on those resurrects the
# bare-first-name fan-out ANALYSIS rejected (docs/ANALYSIS.md "Same-name
# coexistence"). Canonical spellings only; parse-time normalization already ran.
_NEAR_DUP_PREDICATES = frozenset({"name.legal", "name.maiden", "name.aka"})


def local_anchor(captured_at: datetime, tz_offset_minutes: int | None) -> datetime:
    """The capture anchor in the note's LOCAL time.

    created_at round-trips through timestamptz as a UTC instant, so on its own
    it tells the model the wrong calendar day (an evening capture serializes as
    the next UTC day). When the client recorded its offset we re-project the
    instant into that offset, so "today"/"in 3 months" resolve against the
    note's local date (docs/ANALYSIS.md "Temporal model"). Offset absent (older
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


class AnalysisPipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        router: LlmRouter,
        *,
        embedder: EmbedClient | None = None,
        embed_model: str = "",
    ):
        self._maker = maker
        self._router = router
        # Optional on purpose: without an embed client, resolution layer 2 is
        # skipped entirely (no degraded guessing) — the harness and older
        # call sites keep their exact behavior.
        self._embedder = embedder
        self._embed_model = embed_model

    async def analyze_note(self, payload: dict[str, Any]) -> None:
        """Handle an analyze_note job: {note_id}; missing note is a no-op."""
        note_id = str(payload["note_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            note = (
                await session.execute(select(Note).where(Note.id == note_id))
            ).scalar_one_or_none()
            if note is None or note.deleted_at is not None:
                log.info("analysis.skipped", note_id=note_id, reason="missing or deleted")
                return
            body, domain, captured_at = note.body, note.domain_code, note.created_at
            tz_offset = note.tz_offset_minutes
            chunk_rows = (
                await session.execute(
                    select(Chunk.id, Chunk.text, Chunk.source_kind, Attachment.filename)
                    .join(Attachment, Chunk.attachment_id == Attachment.id, isouter=True)
                    .where(Chunk.note_id == note_id)
                    .order_by(Chunk.seq)
                )
            ).all()
        chunks = [_ChunkRef(id=r.id, text=r.text) for r in chunk_rows]
        # Span anchoring (_locate) works on the raw chunk text; only the
        # prompt blocks carry the OCR/caption provenance markers.
        texts = [
            prompt_block(r.text, source_kind=r.source_kind, filename=r.filename) for r in chunk_rows
        ] or [body]

        try:
            result = await self._router.complete(
                "note.extract",
                system=SYSTEM_PROMPT,
                user_text=build_user_prompt(
                    texts, anchor=local_anchor(captured_at, tz_offset), domain=domain
                ),
                json_schema=EXTRACTION_SCHEMA,
                max_tokens=EXTRACT_MAX_TOKENS,
                strength=NOTE_EXTRACT_STRENGTH,
            )
            # Backward-phrase repair needs the note's LOCAL day; without a
            # client offset local_anchor falls back to the stored UTC instant,
            # whose date can be tomorrow for an evening capture. Withhold the
            # anchor in that case so a model-correct date is never clobbered.
            extraction = parse_extraction(
                result.parsed,
                anchor=local_anchor(captured_at, tz_offset) if tz_offset is not None else None,
            )
        except (LlmBadResponseError, ExtractionError, SchemaError) as exc:
            # The adapter already spent its one re-ask: retrying the job would
            # just re-bill the same garbage. A SchemaError (the registry the
            # parser normalizes through is missing/malformed) is config drift,
            # not transient — retrying re-runs the paid extraction for nothing,
            # so it is permanent too. Nothing was written.
            raise PermanentJobError(f"note.extract unusable for note {note_id}: {exc}") from exc

        provider, model = self._router.spec("note.extract", NOTE_EXTRACT_STRENGTH)
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await self._apply(
                session,
                note_id=uuid.UUID(note_id),
                note_domain=domain,
                captured_at=captured_at,
                chunks=chunks,
                extraction=extraction,
                extractor=f"{provider}:{model}",
            )
        log.info(
            "analysis.done",
            note_id=note_id,
            facts=len(extraction.facts),
            mentions=len(extraction.mentions),
        )

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
    ) -> None:
        resolved = await self._resolve_entities(
            session, extraction, note_id, note_domain, chunks, captured_at
        )
        anchor_for = await self._rebuild_mentions(
            session, extraction, resolved, note_id, note_domain, chunks
        )
        token_ids = await self._upsert_tokens(
            session, extraction, note_id, note_domain, captured_at, chunks
        )

        touched: set[uuid.UUID] = set()
        for fact in extraction.facts:
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
            if fact_id is not None:
                touched.add(fact_id)
            await session.flush()

        await self._register_declared_aliases(
            session, extraction, resolved, note_id, note_domain, chunks
        )

        # Identity keys this note no longer asserts were removed by the edit:
        # retract quietly — not a conflict, no inbox noise. Pinned facts are
        # human decisions and survive (docs/ANALYSIS.md "Reprocessing"). Derived
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
            .returning(Fact.id, Fact.superseded_by, Fact.valid_from)
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
                .returning(Fact.id, Fact.superseded_by, Fact.valid_from)
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
        await self._reproject_entities(session, resolved)

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

    async def _resolve_entities(
        self,
        session: AsyncSession,
        extraction: Extraction,
        note_id: uuid.UUID,
        note_domain: str,
        chunks: list[_ChunkRef],
        captured_at: datetime,
    ) -> dict[str, ResolvedEntity | None]:
        """Layered resolution for every name the extraction references
        (docs/ANALYSIS.md "Alias resolution & separation"): exact alias, the
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
        conditional, never per-mention (docs/ANALYSIS.md "Model routing &
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
        facts (docs/ANALYSIS.md), never the frozen first-mention surface form."""
        seen: set[uuid.UUID] = set()
        for entity in resolved.values():
            if entity is None or entity.id in seen:
                continue
            seen.add(entity.id)
            await reproject_canonical_name(session, entity.id)

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
        same-person signal worth auto-suggesting (docs/ANALYSIS.md "Alias
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
                # merge card out across same-named people (docs/ANALYSIS.md
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
        # (docs/ANALYSIS.md "Domains and the firewall", "Facts").
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
        citation never leaves the fact's scope (docs/ANALYSIS.md "Mixed-domain
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
        # A still-future fact is `expected`, never an asserted past event — the
        # anchor is the note's capture time (docs/ANALYSIS.md "Temporal model").
        fact = normalize_future_assertion(fact, captured_at)
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
            # Same identity key, same value: refresh the rendering and
            # provenance in place — citations survive, no chain link, and no
            # repeated promotion review on re-analysis.
            fact_id = uuid.UUID(decision.refresh_id)
            values: dict[str, Any] = {
                "statement": fact.statement,
                "extractor": extractor,
                "prompt_version": PROMPT_VERSION,
                "confidence": fact.confidence,
            }
            # This note DIRECTLY asserts what so far was only a derived shadow of
            # another note's edge: adopt it as a primary fact owned by THIS note,
            # so the human-stated claim survives deletion of the source that
            # first reflected it (red-team Finding 1). Without this, "Celine's
            # spouse is Jeff" in its own note would silently ride Jeff's note and
            # vanish when his note is deleted.
            refreshed = next((e for e in existing if e.id == decision.refresh_id), None)
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
        new_inverse_id: uuid.UUID | None = None
        if (
            fact.kind == "relationship"
            and object_entity is not None
            and decision.insert_status == "active"
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
            session.add(
                ReviewItem(
                    kind=decision.review_kind,
                    payload={
                        "fact_a": decision.conflicting_id,
                        "fact_b": str(new_fact_id),
                        "predicate": fact.predicate,
                        "note_id": str(note_id),
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
        stream, marked derived (docs/research/fix-options/2). Returns the new
        inverse fact id, or None when nothing was written (unknown predicate or
        the cross-subject gate fired)."""
        inverse_pred = inverse_predicate(fact.predicate)
        if inverse_pred is None:
            return None  # not a relation we know how to reciprocate — safe default

        # Cross-subject firewall gate: an inverse lands a fact on the OBJECT's
        # stream. If that object is a DISTINCT security subject, auto-writing it
        # would attribute knowledge across a subject boundary — a leak. Propose
        # it to the review inbox and write nothing (docs/research/fix-options/2,
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
