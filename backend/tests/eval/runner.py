"""Run one corpus case through the real production chain against real Grok.

extract -> integrate (graph-aware) -> plan_intent. Intent-level (no DB): the
case supplies its own `graph_context` string, so this runs anywhere the xAI
token is set, no Postgres needed. The committed-fact path (value_json -> DB) is
already covered deterministically by test_apply_intent_pg; here we exercise the
MODEL's judgment, which is where the production defects lived.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jbrain.analysis.arbiter import ArbiterPlan, compute_signals, plan_intent
from jbrain.analysis.integrate import Integrator
from jbrain.analysis.intent import IntegrationIntent
from jbrain.analysis.pipeline import _extract_note
from jbrain.llm import LlmRouter
from tests.eval.cases import Case, DbCommit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.embed import EmbedClient

_OWNER_LINE = "Owner/author: entity id 'owner-1' name 'Me' (Person)."


def _graph_context(case: Case) -> str:
    # Production's build_graph_context ALWAYS names the owner (get_or_create_me),
    # so the agent can resolve first person to owner-1. Mirror that: inject the
    # owner line unless the case already seeds it.
    ctx = case.graph_context.strip()
    if "owner-1" in ctx:
        return ctx
    return _OWNER_LINE if not ctx else f"{_OWNER_LINE}\n{ctx}"


async def run_case(router: LlmRouter, case: Case) -> tuple[IntegrationIntent, ArbiterPlan]:
    anchor = datetime.now(UTC)
    extraction = await _extract_note(
        router,
        [case.note_text],
        domain=case.domain,
        prompt_anchor=anchor,
        parse_anchor=anchor,
        note_id=case.id,
    )
    intent = await Integrator(router).integrate(
        note_id=case.id,
        extraction=extraction,
        graph_context=_graph_context(case),
        schema_version=1,
        note_text=case.note_text,
    )
    plan = plan_intent(intent, compute_signals(intent, [case.note_text]))
    return intent, plan


# --- DB-mode: run the full chain through apply_intent against real Postgres ----


async def _seed_graph(
    session: AsyncSession,
    case: Case,
    owner_id: uuid.UUID,
    owner_subject_id: uuid.UUID | None,
    prior_note_id: uuid.UUID,
) -> tuple[dict[str, str], list[tuple[str, str, str, uuid.UUID]]]:
    """Materialize the case's `seed` block as real rows so the run resolves
    against known entities. Returns (symbolic_id -> real UUID) and the prior
    facts to watch for supersession as (symbolic, entity_name, predicate, id)."""
    from jbrain.analysis.entities import normalize_alias
    from jbrain.models.analysis import Entity, EntityAlias, Fact

    seeded: dict[str, str] = {}
    names: dict[str, str] = {}
    subjects: dict[str, uuid.UUID | None] = {}
    for ent in case.seed:
        if ent.owner:
            seeded[ent.id] = str(owner_id)
            names[ent.id] = "Me"
            subjects[ent.id] = owner_subject_id
            continue
        subjects[ent.id] = None
        row = Entity(
            id=uuid.uuid4(),
            kind=ent.kind,
            canonical_name=ent.name,
            status="confirmed",
            domain_code=ent.domain,
        )
        session.add(row)
        for alias in (ent.name, *ent.aliases):
            session.add(
                EntityAlias(
                    id=uuid.uuid4(),
                    entity_id=row.id,
                    alias=alias,
                    alias_norm=normalize_alias(alias),
                    domain_code=ent.domain,
                )
            )
        seeded[ent.id] = str(row.id)
        names[ent.id] = ent.name
    await session.flush()

    watched: list[tuple[str, str, str, uuid.UUID]] = []
    for ent in case.seed:
        for sf in ent.facts:
            fact = Fact(
                id=uuid.uuid4(),
                entity_id=uuid.UUID(seeded[ent.id]),
                # Owner facts carry the owner's subject_id; the supersession
                # candidate query matches on it, so a NULL here would hide the
                # prior edge and the new note would never supersede it.
                subject_id=subjects[ent.id],
                object_entity_id=(uuid.UUID(seeded[sf.object]) if sf.object else None),
                predicate=sf.predicate,
                qualifier=sf.qualifier,
                kind=sf.kind,
                statement=f"seed: {ent.name}.{sf.predicate}",
                value_json=({"value": sf.value} if sf.value is not None else None),
                assertion=sf.assertion,
                status="active",
                valid_from=(datetime.fromisoformat(sf.valid_from) if sf.valid_from else None),
                reported_at=datetime.now(UTC),
                note_id=prior_note_id,
                extractor="eval:seed",
                prompt_version="seed",
                domain_code=ent.domain,
            )
            session.add(fact)
            watched.append((ent.id, names[ent.id], sf.predicate, fact.id))
    await session.flush()
    return seeded, watched


async def run_case_db(
    router: LlmRouter,
    case: Case,
    *,
    maker: async_sessionmaker[AsyncSession],
    tmp_path: object,
    embedder: EmbedClient | None = None,
    embed_model: str = "",
    canonicalize: bool = False,
) -> DbCommit:
    """Run a case through the full production chain — extract → integrate →
    [canonicalize] → plan_intent → apply_intent → COMMIT — against real Postgres,
    then read the committed graph back into a DbCommit for check_case_db. Reuses
    the SAME two Grok calls as run_case (token-neutral); only the testcontainer is
    new. With `canonicalize`, an embedder + the predicate_canonicalization setting
    must be live so unknown predicates are matched against the canonical index."""
    from sqlalchemy import select

    from jbrain.analysis.entities import get_or_create_me
    from jbrain.analysis.graph_context import build_graph_context
    from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
    from jbrain.db.session import scoped_session
    from jbrain.ingest.chunker import PARAGRAPH
    from jbrain.models.notes import Chunk
    from jbrain.queue import SYSTEM_CTX
    from jbrain.settings_store import SqlSettingsStore
    from tests.integration.test_extraction_pg import ingest, make_note

    pipeline = AnalysisPipeline(
        maker, router, embedder=embedder, embed_model=embed_model, settings=SqlSettingsStore(maker)
    )
    note_id = await make_note(maker, domain=case.domain, body=case.note_text)
    await ingest(maker, note_id, tmp_path)
    prior_note = await make_note(maker, domain=case.domain, body="seed: prior knowledge")

    # Seed known entities/facts and resolve the owner, all under SYSTEM_CTX (as
    # production integrate_note does — RLS does not scope SYSTEM_CTX).
    async with scoped_session(maker, SYSTEM_CTX) as session:
        owner = await get_or_create_me(session)
        seeded_ids, watched = await _seed_graph(
            session, case, owner.id, owner.subject_id, uuid.UUID(prior_note)
        )
    owner_id = str(owner.id)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        rows = (
            await session.execute(
                select(Chunk.id, Chunk.text)
                .where(Chunk.note_id == note_id, Chunk.granularity == PARAGRAPH)
                .order_by(Chunk.seq)
            )
        ).all()
    chunks = [_ChunkRef(id=r.id, text=r.text) for r in rows]
    texts = [r.text for r in rows] or [case.note_text]

    anchor = datetime.now(UTC)
    extraction = await _extract_note(
        router,
        texts,
        domain=case.domain,
        prompt_anchor=anchor,
        parse_anchor=anchor,
        note_id=note_id,
    )
    async with scoped_session(maker, SYSTEM_CTX) as session:
        graph_context = await build_graph_context(
            session,
            owner_id=owner.id,
            mentions=extraction.mentions,
            note_domain=case.domain,
            embedder=embedder,
            embed_model=embed_model,
        )
    intent = await Integrator(router).integrate(
        note_id=note_id,
        extraction=extraction,
        graph_context=graph_context,
        schema_version=1,
        note_text="\n\n".join(texts),
    )
    # Canonicalize unknown predicates before the arbiter keys facts (Phase 3 §3.1);
    # self-gates on the setting + embedder, so it's inert unless --canon armed both.
    if canonicalize:
        await pipeline.canonicalize_intent(intent, note_domain=case.domain)
    plan = plan_intent(intent, compute_signals(intent, texts))

    async with scoped_session(maker, SYSTEM_CTX) as session:
        await pipeline.apply_intent(
            session,
            note_id=uuid.UUID(note_id),
            note_domain=case.domain,
            captured_at=anchor,
            chunks=chunks,
            intent=intent,
            plan=plan,
            title=extraction.title,
            tags=extraction.tags,
            extractor="eval:db",
            dropped_facts=extraction.dropped_facts,
        )

    return await _read_commit(maker, note_id, owner_id, seeded_ids, watched)


async def _read_commit(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    owner_id: str,
    seeded_ids: dict[str, str],
    watched: list[tuple[str, str, str, uuid.UUID]],
) -> DbCommit:
    from sqlalchemy import select

    from jbrain.db.session import scoped_session
    from jbrain.models.analysis import Entity, Fact, ReviewItem
    from jbrain.queue import SYSTEM_CTX
    from tests.eval.cases import CommittedFact, ReviewCard, SeededFactState

    async with scoped_session(maker, SYSTEM_CTX) as session:
        fact_rows = list(
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
        ref_ids = {f.entity_id for f in fact_rows} | {
            f.object_entity_id for f in fact_rows if f.object_entity_id is not None
        }
        ent_rows = (
            list(
                (await session.execute(select(Entity).where(Entity.id.in_(ref_ids))))
                .scalars()
                .all()
            )
            if ref_ids
            else []
        )
        names = {str(e.id): e.canonical_name for e in ent_rows}
        # Any review item tied to this note, regardless of kind: a held fact can
        # carry a low_confidence_inference card (payload.fact_id) OR a conflict
        # card (attribute_conflict etc., payload.fact_b = the held new fact).
        cards = list(
            (
                await session.execute(
                    select(ReviewItem).where(
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        watched_rows = (
            {
                wid: (await session.execute(select(Fact).where(Fact.id == wid))).scalar_one()
                for *_x, wid in watched
            }
            if watched
            else {}
        )

    facts = tuple(
        CommittedFact(
            id=str(f.id),
            entity_id=str(f.entity_id),
            entity_name=names.get(str(f.entity_id), ""),
            predicate=f.predicate,
            qualifier=f.qualifier or "",
            kind=f.kind,
            value_json=f.value_json,
            assertion=f.assertion,
            status=f.status,
            domain_code=f.domain_code,
            object_entity_id=(str(f.object_entity_id) if f.object_entity_id else None),
            object_name=(names.get(str(f.object_entity_id)) if f.object_entity_id else None),
        )
        for f in fact_rows
    )
    review_fact_ids = frozenset(
        str(c.payload[k]) for c in cards for k in ("fact_id", "fact_b") if c.payload.get(k)
    )

    def _state(sym: str, name: str, pred: str, row: Fact) -> SeededFactState:
        return SeededFactState(
            entity_symbolic=sym,
            entity_name=name,
            predicate=pred,
            status=row.status,
            superseded_by=(str(row.superseded_by) if row.superseded_by else None),
            valid_to=(row.valid_to.isoformat() if row.valid_to else None),
        )

    seeded_facts = tuple(
        _state(sym, name, pred, watched_rows[wid]) for sym, name, pred, wid in watched
    )
    review_cards = tuple(
        ReviewCard(
            kind=c.kind,
            predicate=c.payload.get("predicate"),
            suggestions=tuple((s["name"], s["score"]) for s in c.payload.get("suggestions", [])),
        )
        for c in cards
        if c.kind == "new_predicate"
    )
    return DbCommit(
        owner_id=owner_id,
        note_id=note_id,
        seeded_ids=seeded_ids,
        facts=facts,
        entities=names,
        review_fact_ids=review_fact_ids,
        seeded_facts=seeded_facts,
        review_cards=review_cards,
    )
