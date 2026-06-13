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
from jbrain.models.analysis import Entity, Fact
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


async def test_apply_intent_excludes_cross_subject_review_fact(maker, tmp_path):  # noqa: F811
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
    # The cross-subject fact (review-held) is excluded by commit_only; only the
    # clean fact committed.
    assert len(facts) == 1
    assert facts[0].statement == "Globex is in tech"


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
