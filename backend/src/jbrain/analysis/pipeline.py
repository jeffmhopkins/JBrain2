"""The analyze_note job handler: one note.extract call -> facts, entities,
mentions, temporal tokens, review items, note_analysis (docs/ANALYSIS.md).

Failure contract: transient LLM faults propagate and ride the queue's normal
retry backoff; an extraction that stayed malformed through the adapter's
re-ask is a PermanentJobError. All writes happen in one transaction, so a
failed run never partial-writes facts, and re-analysis is idempotent: facts
upsert on the structural identity key, mentions rebuild wholesale (the chunks
pattern), tokens are reused by (phrase, resolved value).

TODO(analysis): mixed-domain notes should also derive per-domain chunks so a
citation never crosses the firewall (docs/ANALYSIS.md "Mixed-domain notes");
until then a ratcheted fact may cite a chunk in the note's capture domain,
which RLS simply hides from narrower scopes.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.display import (
    ambiguous_display,
    collision_display,
    mark_snippet,
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
    build_disambiguation_prompt,
    create_provisional,
    parse_disambiguation,
    resolve_entity,
)
from jbrain.analysis.extraction import (
    ExtractedFact,
    Extraction,
    ExtractionError,
    normalize_future_assertion,
    parse_extraction,
    ratchet_domain,
)
from jbrain.analysis.prompt import (
    EXTRACTION_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from jbrain.analysis.supersession import Candidate, Decision, FactView, decide, is_functional
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
from jbrain.models.notes import Chunk, Note
from jbrain.queue import SYSTEM_CTX, PermanentJobError

log = structlog.get_logger()

EXTRACT_MAX_TOKENS = 8192

_DB_LINK_METHODS = frozenset({"exact_alias", "embedding", "llm", "human"})

# Below embedding auto-link confidence on purpose: a cheap-model verdict over
# near-tie candidates is real evidence, not certainty.
LLM_LINK_CONFIDENCE = 0.8


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
                    select(Chunk.id, Chunk.text).where(Chunk.note_id == note_id).order_by(Chunk.seq)
                )
            ).all()
        chunks = [_ChunkRef(id=r.id, text=r.text) for r in chunk_rows]
        texts = [c.text for c in chunks] or [body]

        try:
            result = await self._router.complete(
                "note.extract",
                system=SYSTEM_PROMPT,
                user_text=build_user_prompt(
                    texts, anchor=local_anchor(captured_at, tz_offset), domain=domain
                ),
                json_schema=EXTRACTION_SCHEMA,
                max_tokens=EXTRACT_MAX_TOKENS,
            )
            extraction = parse_extraction(result.parsed)
        except (LlmBadResponseError, ExtractionError) as exc:
            # The adapter already spent its one re-ask: retrying the job would
            # just re-bill the same garbage. Nothing was written.
            raise PermanentJobError(f"note.extract unusable for note {note_id}: {exc}") from exc

        provider, model = self._router.spec("note.extract")
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

        # Identity keys this note no longer asserts were removed by the edit:
        # retract quietly — not a conflict, no inbox noise. Pinned facts are
        # human decisions and survive (docs/ANALYSIS.md "Reprocessing").
        sweep = (
            update(Fact)
            .where(
                Fact.note_id == note_id,
                Fact.pinned.is_(False),
                Fact.status.in_(("active", "pending_review")),
            )
            .values(status="retracted")
        )
        if touched:
            sweep = sweep.where(Fact.id.not_in(touched))
        await session.execute(sweep)

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
            )
            for f in rows
        ]

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

        fact_domain, needs_promotion = ratchet_domain(fact.domain or note_domain, note_domain)

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

        if decision.refresh_id is not None:
            # Same identity key, same value: refresh the rendering and
            # provenance in place — citations survive, no chain link, and no
            # repeated promotion review on re-analysis.
            fact_id = uuid.UUID(decision.refresh_id)
            await session.execute(
                update(Fact)
                .where(Fact.id == fact_id)
                .values(
                    statement=fact.statement,
                    extractor=extractor,
                    prompt_version=PROMPT_VERSION,
                    confidence=fact.confidence,
                )
            )
            return fact_id

        anchor = anchor_for.get(fact.entity_ref)
        chunk_id = anchor[0] if anchor else (chunks[0].id if chunks else None)
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
