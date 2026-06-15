"""apply_intent (Wave 1 Track A, A1b-ii-1): an arbiter-approved IntegrationIntent
committed through the existing deterministic _apply, against real Postgres. The
LLM is not used — apply_intent consumes a pre-built intent + plan. Reuses the
proven note/ingest helpers from test_extraction_pg.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.intent import (
    AttestedSpan,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
)
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import scoped_session
from jbrain.ingest.chunker import PARAGRAPH
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact, ReviewItem
from jbrain.models.notes import Chunk
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_SURFACE = ConfidenceSignals(surface_attested=True, predicate_known=True, is_supersede=False)


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    # apply_intent never calls the LLM; a stub router satisfies the constructor.
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(maker, router)


async def _load_chunks(maker, note_id: str) -> list[_ChunkRef]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        rows = (
            await session.execute(
                select(Chunk.id, Chunk.text)
                .where(Chunk.note_id == note_id, Chunk.granularity == PARAGRAPH)
                .order_by(Chunk.seq)
            )
        ).all()
    return [_ChunkRef(id=r.id, text=r.text) for r in rows]


def _fact(entity_ref: str, **kw) -> IntentFact:
    base: dict[str, Any] = dict(
        predicate="industry",
        qualifier="",
        kind="attribute",
        statement="Globex is in tech",
        value_json=None,
        assertion="asserted",
        object_entity_ref=None,
        temporal=None,
        attested_span=AttestedSpan("c", "Globex"),
        self_confidence=0.95,
        inferred=False,
    )
    base.update(kw)
    return IntentFact(entity_ref=entity_ref, **base)


def _intent(note_id: str, resolutions, facts) -> IntegrationIntent:
    return IntegrationIntent(
        note_id=note_id,
        schema_version=1,
        prompt_version="v13",
        integrator_version="i1",
        entity_resolutions=resolutions,
        facts=facts,
    )


async def _run(maker, note_id, intent, plan, *, tmp_path) -> None:  # noqa: F811
    chunks = await _load_chunks(maker, note_id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await _pipeline(maker).apply_intent(
            session,
            note_id=uuid.UUID(note_id),
            note_domain="general",
            captured_at=datetime.now(UTC),
            chunks=chunks,
            intent=intent,
            plan=plan,
            title="t",
            tags=["work"],
            extractor="test:fake",
        )


async def test_apply_intent_commits_a_surface_fact_and_mints_entity(maker, tmp_path):  # noqa: F811
    note_id = await make_note(maker, domain="general", body="Notes about Globex and its plans.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="new", new_kind="Organization", new_name="Globex"
            )
        ],
        [_fact("m1")],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
        ents = (
            (await session.execute(select(Entity).where(Entity.canonical_name == "Globex")))
            .scalars()
            .all()
        )
    assert len(ents) == 1  # the new-mode resolution minted a provisional entity
    assert len(facts) == 1
    assert facts[0].predicate == "industry"
    assert facts[0].status == "active"


async def test_apply_intent_holds_cross_subject_review_fact(maker, tmp_path):  # noqa: F811
    note_id = await make_note(maker, domain="general", body="Globex and Initech notes.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="new", new_kind="Organization", new_name="Globex"
            ),
            EntityResolution(
                mention_ref="m2",
                mode="new",
                new_kind="Organization",
                new_name="Initech",
                cross_subject=True,
            ),
        ],
        [_fact("m1"), _fact("m2", statement="Initech is in tech")],
    )
    sig = {0: _SURFACE, 1: _SURFACE}
    plan = plan_intent(intent, signals=sig)
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
        cards = (
            (
                await session.execute(
                    select(ReviewItem).where(
                        ReviewItem.kind == "low_confidence_inference",
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    # The clean fact commits active; the cross-subject fact is HELD as a
    # pending_review row (A1b-ii-2) — not dropped — and its card links to it.
    by_stmt = {f.statement: f for f in facts}
    assert by_stmt["Globex is in tech"].status == "active"
    held = by_stmt["Initech is in tech"]
    assert held.status == "pending_review"
    assert held.pinned is False
    assert len(cards) == 1
    assert cards[0].payload["reasons"] == ["cross_subject_link"]
    assert cards[0].payload["statement"] == "Initech is in tech"
    assert cards[0].payload["fact_id"] == str(held.id)  # card → row linkage
    # The structured proposal the card renders as `predicate → value`, so the
    # owner sees the fact, not only the prose statement.
    assert cards[0].payload["predicate"] == "industry"
    assert "value_json" in cards[0].payload
    # The verbose process trace travels in the payload: the three pipeline stages
    # the review UI plays back, with the arbiter stage naming the held reason.
    trace = cards[0].payload["trace"]
    assert [s["key"] for s in trace["stages"]] == ["extraction", "integration", "arbiter"]
    arbiter_rows = dict(r for r in trace["stages"][2]["rows"])
    assert "cross_subject_link" in arbiter_rows["status"]


async def test_apply_intent_holds_below_threshold_fact_decide_would_commit(maker, tmp_path):  # noqa: F811
    # A low-weight fact (no cross-subject) the weight model holds below threshold:
    # routed to _insert_held_fact, it must land pending_review even though decide()
    # — fed the same fact — would have inserted it ACTIVE (no existing head).
    note_id = await make_note(maker, domain="general", body="Globex notes.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="new", new_kind="Organization", new_name="Globex"
            )
        ],
        [_fact("m1", inferred=True, self_confidence=0.2)],
    )
    # Inferred + low self-confidence + no surface signal → weight under the commit
    # threshold → held (below_threshold), not active.
    plan = plan_intent(intent, signals={0: ConfidenceSignals(False, True, False)})
    assert plan.to_review and not plan.to_commit
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
    assert len(facts) == 1 and facts[0].status == "pending_review"


async def _seed_entity(maker, name: str, *, domain: str = "general") -> str:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        ent = Entity(
            kind="Organization", canonical_name=name, status="confirmed", domain_code=domain
        )
        session.add(ent)
        await session.flush()
        return str(ent.id)


async def _seed_active_fact(maker, *, predicate: str, statement: str, domain: str = "general"):  # noqa: F811
    """An existing active entity + fact (from a PRIOR note), to prove a held fact
    never supersedes it. The prior note is what owns the active fact's note_id."""
    prior_note = await make_note(maker, domain=domain, body="prior fact note")
    async with scoped_session(maker, SYSTEM_CTX) as session:
        ent = Entity(
            kind="Organization", canonical_name="Acme", status="confirmed", domain_code=domain
        )
        session.add(ent)
        await session.flush()
        fact = Fact(
            entity_id=ent.id,
            predicate=predicate,
            qualifier="",
            kind="attribute",
            statement=statement,
            assertion="asserted",
            status="active",
            reported_at=datetime.now(UTC),
            note_id=uuid.UUID(prior_note),
            extractor="test:fake",
            prompt_version="v1",
            domain_code=domain,
        )
        session.add(fact)
        await session.flush()
        return str(ent.id), str(fact.id)


async def test_held_fact_does_not_supersede_an_existing_active_fact(maker, tmp_path):  # noqa: F811
    ent_id, active_id = await _seed_active_fact(maker, predicate="industry", statement="Acme: old")
    note_id = await make_note(maker, domain="general", body="Acme notes.")
    await ingest(maker, note_id, tmp_path)
    # Resolve to the EXISTING Acme, cross_subject → the new industry value is held.
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="existing", proposed_entity_id=ent_id, cross_subject=True
            )
        ],
        [_fact("m1", statement="Acme: new")],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        active = (
            await session.execute(select(Fact).where(Fact.id == uuid.UUID(active_id)))
        ).scalar_one()
        held = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
    assert active.status == "active" and active.superseded_by is None  # untouched
    assert len(held) == 1 and held[0].status == "pending_review"
    assert held[0].statement == "Acme: new"


async def test_held_fact_is_idempotent_across_reanalysis(maker, tmp_path):  # noqa: F811
    # Real reprocessing resolves a mention to the SAME existing entity (the agent
    # sees it in graph context, so mode="existing"), so re-running must refresh the
    # held row in place — stable id, one row — not churn a duplicate that orphans
    # the card's fact_id. (mode="new" always mints a fresh entity, committed or
    # held alike, so it is not the reprocessing scenario this guards.)
    ent_id = await _seed_entity(maker, "Initech")
    note_id = await make_note(maker, domain="general", body="Initech notes.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="existing", proposed_entity_id=ent_id, cross_subject=True
            )
        ],
        [_fact("m1", statement="Initech is in tech")],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)  # re-analysis

    async with scoped_session(maker, SYSTEM_CTX) as session:
        held = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
        cards = (
            (
                await session.execute(
                    select(ReviewItem).where(
                        ReviewItem.kind == "low_confidence_inference",
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(held) == 1 and held[0].status == "pending_review"  # not duplicated
    assert len(cards) == 1
    assert cards[0].payload["fact_id"] == str(held[0].id)  # link still valid


async def test_held_health_fact_floors_to_health_on_row_and_card(maker, tmp_path):  # noqa: F811
    # Firewall: a cross-subject health-predicate fact held in a GENERAL note must
    # floor to the health domain on BOTH the pending_review row and its card, so
    # the firewall (RLS) keeps it out of a general-scoped read.
    note_id = await make_note(maker, domain="general", body="Mom's medication notes.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="new", new_kind="Person", new_name="Mom", cross_subject=True
            )
        ],
        [_fact("m1", predicate="medication", statement="Mom takes lisinopril")],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        held = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
        card = (
            await session.execute(
                select(ReviewItem).where(
                    ReviewItem.kind == "low_confidence_inference",
                    ReviewItem.payload["note_id"].astext == note_id,
                )
            )
        ).scalar_one()
    assert len(held) == 1 and held[0].status == "pending_review"
    assert held[0].domain_code == "health"  # floored despite the general note
    assert card.domain_code == "health"  # card rides the same floor — no leak


async def test_apply_intent_commits_fact_value_json(maker, tmp_path):  # noqa: F811
    # End of the value_json path (the v5 fix): a name attribute's bare datum must
    # land on the committed fact row, not regress to the statement sentence.
    note_id = await make_note(maker, domain="general", body="Celine Kitina Hopkins notes.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Celine")],
        [
            _fact(
                "m1",
                predicate="name.full",
                kind="attribute",
                statement="Celine's full name is Celine Kitina Hopkins.",
                value_json={"value": "Celine Kitina Hopkins"},
            )
        ],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        fact = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .one()
        )
    assert fact.status == "active"
    assert fact.value_json == {"value": "Celine Kitina Hopkins"}  # bare value, not the sentence


async def test_value_shape_mismatch_is_logged_not_dropped(maker, tmp_path):  # noqa: F811
    # Phase 1 typed value-shape validation is LOG-ONLY: a ref predicate (spouse)
    # handed a scalar value_json with no object violates its declared shape, so we
    # WARN — but the value commits intact (enforcement/drop is gated on a real-Grok
    # eval proving the conservative validator never false-drops a sound value).
    import structlog.testing

    note_id = await make_note(maker, domain="general", body="A note about Pat.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Pat")],
        [
            _fact(
                "m1",
                predicate="spouse",
                kind="relationship",
                statement="Pat's spouse is Jane",
                value_json={"value": "Jane"},
            )
        ],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    with structlog.testing.capture_logs() as logs:
        await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        fact = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .one()
        )
    assert fact.value_json == {"value": "Jane"}  # log-only: committed, NOT dropped
    assert any(e.get("event") == "analysis.fact_value_shape_mismatch" for e in logs)


async def test_value_shape_mismatch_drops_value_when_enforced(maker, tmp_path):  # noqa: F811
    # Phase 4 ENFORCE: with value_shape_enforce on, the same shape-violating
    # value_json is dropped — the fact still commits (active), on its statement.
    import structlog.testing

    from jbrain.settings_store import VALUE_SHAPE_ENFORCE_KEY, SqlSettingsStore

    await SqlSettingsStore(maker).upsert(SYSTEM_CTX, VALUE_SHAPE_ENFORCE_KEY, True)
    note_id = await make_note(maker, domain="general", body="A note about Pat.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Pat")],
        [
            _fact(
                "m1",
                predicate="spouse",
                kind="relationship",
                statement="Pat's spouse is Jane",
                value_json={"value": "Jane"},
            )
        ],
    )
    plan = plan_intent(intent, signals={0: _SURFACE})
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    pipeline = AnalysisPipeline(maker, router, settings=SqlSettingsStore(maker))
    chunks = await _load_chunks(maker, note_id)
    with structlog.testing.capture_logs() as logs:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            await pipeline.apply_intent(
                session,
                note_id=uuid.UUID(note_id),
                note_domain="general",
                captured_at=datetime.now(UTC),
                chunks=chunks,
                intent=intent,
                plan=plan,
                title="t",
                tags=[],
                extractor="test:fake",
            )

    async with scoped_session(maker, SYSTEM_CTX) as session:
        fact = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .one()
        )
    assert fact.value_json is None  # enforced: the bad value was dropped
    assert fact.status == "active"  # the fact itself is kept (storage invariant)
    assert any(e.get("event") == "analysis.fact_value_shape_dropped" for e in logs)


async def test_apply_intent_rejected_plan_is_a_noop(maker, tmp_path):  # noqa: F811
    note_id = await make_note(maker, domain="general", body="Globex note.")
    await ingest(maker, note_id, tmp_path)
    # A fact referencing a non-existent mention → fatal → rejected plan.
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1", mode="new", new_kind="Organization", new_name="Globex"
            )
        ],
        [_fact("ghost")],
    )
    plan = plan_intent(intent)
    assert plan.rejected
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
    assert facts == []  # nothing written (N5: no partial commit)
