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
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.entities import AmbiguousEntity, ResolvedEntity, resolve_entity
from jbrain.analysis.extraction import (
    ExtractedFact,
    Extraction,
    ExtractionError,
    parse_extraction,
    ratchet_domain,
)
from jbrain.analysis.prompt import (
    EXTRACTION_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from jbrain.analysis.supersession import Candidate, Decision, FactView, decide
from jbrain.db.session import scoped_session
from jbrain.llm import LlmBadResponseError, LlmRouter
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
SNIPPET_CHARS = 240


@dataclass(frozen=True)
class _ChunkRef:
    id: uuid.UUID
    text: str


def _locate(surface: str, chunks: list[_ChunkRef]) -> tuple[uuid.UUID, int, int] | None:
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
    def __init__(self, maker: async_sessionmaker[AsyncSession], router: LlmRouter):
        self._maker = maker
        self._router = router

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
                user_text=build_user_prompt(texts, anchor=captured_at, domain=domain),
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
        resolved = await self._resolve_entities(session, extraction, note_id, note_domain)
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
    ) -> dict[str, ResolvedEntity | None]:
        """Layer-1 resolution for every name the extraction references.

        Ambiguous names resolve to None (no link) and file one deduplicated
        ambiguous_mention review item.
        """
        kind_hints = {m.name: m.kind for m in extraction.mentions}
        names: list[str] = []
        for mention in extraction.mentions:
            if mention.name not in names:
                names.append(mention.name)
        for fact in extraction.facts:
            for ref in (fact.entity_ref, fact.object_entity_ref):
                if ref and ref not in names:
                    names.append(ref)

        resolved: dict[str, ResolvedEntity | None] = {}
        for name in names:
            outcome = await resolve_entity(
                session, name, kind_hint=kind_hints.get(name, "Thing"), domain=note_domain
            )
            if isinstance(outcome, AmbiguousEntity):
                resolved[name] = None
                await self._file_ambiguous_review(
                    session, name, note_id, note_domain, outcome.candidate_ids
                )
            else:
                resolved[name] = outcome
        return resolved

    async def _file_ambiguous_review(
        self,
        session: AsyncSession,
        name: str,
        note_id: uuid.UUID,
        note_domain: str,
        candidate_ids: list[uuid.UUID],
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
    ) -> dict[str, uuid.UUID]:
        """Delete + insert this note's mentions (the chunks pattern from
        ingest); returns name -> anchoring chunk for fact provenance."""
        await session.execute(delete(EntityMention).where(EntityMention.note_id == note_id))
        anchor_for: dict[str, uuid.UUID] = {}
        for mention in extraction.mentions:
            entity = resolved.get(mention.name)
            located = _locate(mention.surface_text, chunks)
            if located is not None and mention.name not in anchor_for:
                anchor_for[mention.name] = located[0]
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
                    link_method="exact_alias",
                    confidence=1.0,
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
    ) -> list[FactView]:
        stmt = select(Fact).where(
            Fact.entity_id == entity_id,
            Fact.predicate == predicate,
            Fact.qualifier == qualifier,
            Fact.subject_id == subject_id if subject_id else Fact.subject_id.is_(None),
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            FactView(
                id=str(f.id),
                kind=f.kind,
                statement=f.statement,
                value_json=f.value_json,
                object_entity_id=str(f.object_entity_id) if f.object_entity_id else None,
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
        anchor_for: dict[str, uuid.UUID],
        note_id: uuid.UUID,
        note_domain: str,
        captured_at: datetime,
        chunks: list[_ChunkRef],
        extractor: str,
    ) -> uuid.UUID | None:
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
            valid_from=valid_from,
            valid_to=valid_to,
            reported_at=captured_at,
        )
        existing = await self._existing_facts(
            session, entity.id, fact.predicate, fact.qualifier, entity.subject_id
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

        chunk_id = anchor_for.get(fact.entity_ref) or (chunks[0].id if chunks else None)
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
        await self._apply_decision_side_effects(
            session, decision, new_fact.id, fact, fact_domain, note_id, valid_from
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
                        **decision.review_extra,
                    },
                    domain_code=fact_domain,
                )
            )
