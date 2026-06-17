"""apply_intent (Wave 1 Track A, A1b-ii-1): an arbiter-approved IntegrationIntent
committed through the existing deterministic _apply, against real Postgres. The
LLM is not used — apply_intent consumes a pre-built intent + plan. Reuses the
proven note/ingest helpers from test_extraction_pg.
"""

import hashlib
import random
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.intent import (
    AttestedSpan,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
)
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.analysis.predicates import raw_descriptor
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import scoped_session
from jbrain.ingest.chunker import PARAGRAPH
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact, ReviewItem
from jbrain.models.notes import Chunk
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import PREDICATE_CANON_KEY, SqlSettingsStore
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


_EMBED_MODEL = "test-embed-v1"


def _vec(t: str) -> list[float]:
    rng = random.Random(int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big"))
    return [rng.uniform(-1, 1) for _ in range(384)]


class _FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_vec(t) for t in texts]


def _embed_pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    # apply_intent's inference-card pass weights relation candidates through the
    # embedder when the canonicalization setting is on; wire both up.
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(
        maker,
        router,
        embedder=_FakeEmbed(),
        embed_model=_EMBED_MODEL,
        settings=SqlSettingsStore(maker),
    )


async def _seed_canonical(maker, name: str, embedding_text: str) -> None:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind, embedding, embedding_model)"
                " VALUES (:n, 'd', 'text', 'attribute', cast(:emb AS vector), :model)"
                " ON CONFLICT (canonical_name) DO UPDATE SET"
                " embedding = cast(:emb AS vector), embedding_model = :model"
            ),
            {
                "n": name,
                "emb": "[" + ",".join(map(str, _vec(embedding_text))) + "]",
                "model": _EMBED_MODEL,
            },
        )


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


async def _run(
    maker,  # noqa: F811
    note_id,
    intent,
    plan,
    *,
    tmp_path,
    dropped_facts: int = 0,
    pipeline=None,
) -> None:
    chunks = await _load_chunks(maker, note_id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await (pipeline or _pipeline(maker)).apply_intent(
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
            dropped_facts=dropped_facts,
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


async def test_name_fact_recovers_its_value_from_the_statement(maker, tmp_path):  # noqa: F811
    # The model left value_json null and only a prose statement — _shape_check
    # recovers the bare name so the page shows the value, not the sentence.
    note_id = await make_note(maker, domain="general", body="My name is Jeff Hopkins.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Jeff")],
        [
            _fact(
                "m1",
                predicate="name.full",
                qualifier="",
                kind="attribute",
                statement="Full name is Jeff Hopkins.",
                value_json=None,
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
    assert fact.value_json == {"value": "Jeff Hopkins"}


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
    # The fact's modality rides along so the card can correct it in place.
    assert cards[0].payload["assertion"] == "asserted"
    # The verbose process trace travels in the payload: the three pipeline stages
    # the review UI plays back, with the arbiter stage naming the held reason.
    trace = cards[0].payload["trace"]
    assert [s["key"] for s in trace["stages"]] == ["extraction", "integration", "arbiter"]
    arbiter_rows = dict(r for r in trace["stages"][2]["rows"])
    assert "cross_subject_link" in arbiter_rows["status"]


async def test_held_card_carries_weighted_predicate_suggestions(maker, tmp_path):  # noqa: F811
    # With the canonicalization setting on and an embedder wired, a held fact's
    # inference card carries the canonicals nearest its relation — the weighted
    # list the correct-in-place predicate picker offers. Seed 'sector' at the
    # exact vector the held predicate's descriptor embeds to (cosine 1 → top).
    await SqlSettingsStore(maker).upsert(SYSTEM_CTX, PREDICATE_CANON_KEY, True)
    await _seed_canonical(
        maker, "sector", raw_descriptor("industry", "Initech is in tech", "attribute")
    )
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
    plan = plan_intent(intent, signals={0: _SURFACE, 1: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path, pipeline=_embed_pipeline(maker))

    async with scoped_session(maker, SYSTEM_CTX) as session:
        card = (
            await session.execute(
                select(ReviewItem).where(
                    ReviewItem.kind == "low_confidence_inference",
                    ReviewItem.payload["note_id"].astext == note_id,
                )
            )
        ).scalar_one()
    suggestions = card.payload["predicate_suggestions"]
    assert isinstance(suggestions, list) and suggestions
    names = {s["name"] for s in suggestions}
    assert "sector" in names
    # The strongest match is a near-perfect score (the seeded identical vector).
    top = max(suggestions, key=lambda s: s["score"])
    assert top["name"] == "sector" and top["score"] > 0.99


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


async def test_enum_value_is_coerced_to_its_member(maker, tmp_path):  # noqa: F811
    # The screenshot bug: the integrator wrote its rationale into a gender value
    # ("Female (inferred from 'wife')"). gender is a closed enum, so _shape_check
    # coerces the stored datum to the bare member — the proposed-fact panel then
    # reads "female", not the prose.
    note_id = await make_note(maker, domain="general", body="A note about Celine.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Celine")],
        [
            _fact(
                "m1",
                predicate="gender",
                kind="state",
                statement="Female (inferred from 'wife').",
                value_json={"value": "Female (inferred from 'wife')."},
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
    assert fact.value_json == {"value": "female"}  # coerced to the enum member


async def test_inference_card_renders_the_committed_coerced_value(maker, tmp_path):  # noqa: F811
    # The card and the note view must agree: a held gender inference is shape-
    # checked/coerced on the committed row, and the card sources its value from
    # that row — so both surfaces read "female", never the model's raw prose.
    note_id = await make_note(maker, domain="general", body="A note about Celine.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Celine")],
        [
            _fact(
                "m1",
                predicate="gender",
                kind="state",
                statement="Celine's gender is female.",
                value_json={"value": "Female (inferred from 'wife')."},
                inferred=True,
                self_confidence=0.6,
                attested_span=None,
            )
        ],
    )
    # Inferred (not surface-attested): weight is capped to 0.6 < the 0.7 state
    # threshold, so the fact is held and an inference card is filed.
    plan = plan_intent(intent, signals={0: ConfidenceSignals(False, True, False)})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        fact = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .one()
        )
        card = (
            (
                await session.execute(
                    select(ReviewItem).where(
                        ReviewItem.kind == "low_confidence_inference",
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            )
            .scalars()
            .one()
        )
    assert fact.status == "pending_review"
    assert fact.value_json == {"value": "female"}
    # The card mirrors the committed row, not the raw planned value.
    assert card.payload["value_json"] == {"value": "female"}
    assert card.payload["fact_id"] == str(fact.id)
    # gender is a closed enum, so the card carries its members for the correct-in-
    # place picker — approve as-is, or pick another member to file a correction.
    assert card.payload["enum_values"] == ["male", "female", "unknown"]


async def test_nicknames_for_different_audiences_coexist(maker, tmp_path):  # noqa: F811
    # Agglutination by audience: name.nickname is non-functional and keyed by its
    # qualifier, so "Sammy" (general) and "Mom" (kids) are DISTINCT addresses that
    # both commit — neither overwrites the other (no attribute_collision).
    note_id = await make_note(maker, domain="general", body="Celine, aka Sammy; kids call her Mom.")
    await ingest(maker, note_id, tmp_path)
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Person", new_name="Celine")],
        [
            _fact(
                "m1",
                predicate="name.nickname",
                qualifier="",
                kind="attribute",
                statement="Celine goes by Sammy.",
                value_json={"value": "Sammy"},
            ),
            _fact(
                "m1",
                predicate="name.nickname",
                qualifier="kids",
                kind="attribute",
                statement="Celine's kids call her Mom.",
                value_json={"value": "Mom"},
            ),
        ],
    )
    plan = plan_intent(intent, signals={0: _SURFACE, 1: _SURFACE})
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
    by_qual = {f.qualifier: f for f in facts}
    assert by_qual[""].value_json == {"value": "Sammy"} and by_qual[""].status == "active"
    assert by_qual["kids"].value_json == {"value": "Mom"} and by_qual["kids"].status == "active"


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


async def _truncation_cards(maker, note_id):  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return (
            (
                await session.execute(
                    select(ReviewItem).where(
                        ReviewItem.kind == "extraction_truncated",
                        ReviewItem.status == "open",
                        # Scope to THIS note — the module shares a DB, so an
                        # earlier over-cap test's open card must not leak in.
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_over_cap_note_files_extraction_truncated_card(maker, tmp_path):  # noqa: F811
    # W0 regression: an upstream per-note cap that dropped facts must surface the
    # extraction_truncated card. apply_intent gets the drop count threaded in
    # (the real integrate_note source is extraction.dropped_facts); before the
    # fix plan_to_extraction reset it to 0 and the card was never filed.
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
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path, dropped_facts=3)

    cards = await _truncation_cards(maker, note_id)
    assert len(cards) == 1
    assert cards[0].payload["note_id"] == note_id


async def test_under_cap_note_files_no_truncation_card(maker, tmp_path):  # noqa: F811
    # The mirror case: no upstream drop → no card. Guards against a card filed on
    # every integrate regardless of truncation.
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
    await _run(maker, note_id, intent, plan, tmp_path=tmp_path, dropped_facts=0)

    assert await _truncation_cards(maker, note_id) == []
