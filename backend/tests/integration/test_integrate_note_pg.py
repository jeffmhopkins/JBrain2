"""integrate_note end to end (Wave 1 Track B wire-up) against real Postgres:
extract → Integrator → plan_intent → apply_intent → integration_state. Both model
calls (note.extract, integrate.note) are faked with scripted JSON. Reuses the
proven note/ingest helpers from test_extraction_pg.
"""

import json
import uuid

import pytest
from sqlalchemy import select

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact, ReviewItem
from jbrain.models.notes import Note
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# 1st model call (note.extract) → a valid Extraction; 2nd (integrate.note) → an
# IntegrationIntent. A short note is one extract group → one extract call, so the
# scripted responses line up by call order.
_EXTRACT = json.dumps(
    {
        "title": "Work",
        "tags": ["work", "career", "tech"],
        "mentions": [{"name": "Globex", "kind": "Organization", "surface_text": "Globex"}],
        "facts": [
            {
                "entity_ref": "Globex",
                "predicate": "industry",
                "qualifier": "",
                "kind": "attribute",
                "statement": "Globex is in tech",
                "value_json": None,
                "assertion": "asserted",
                "object_entity_ref": None,
                "domain": "general",
                "temporal": None,
            }
        ],
        "temporal_tokens": [],
    }
)
_INTENT = json.dumps(
    {
        "resolutions": [
            {
                "mention_ref": "Globex",
                "mode": "new",
                "new_kind": "Organization",
                "new_name": "Globex",
            }
        ],
        "facts": [
            {
                "entity_ref": "Globex",
                "predicate": "industry",
                "kind": "attribute",
                "assertion": "asserted",
                "statement": "Globex is in tech",
                "self_confidence": 0.95,
                "chunk_id": "x",
                "surface": "Globex",  # present in the note body → surface-attested → commit
            }
        ],
    }
)


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    fake = FakeLlmClient(responses=[_EXTRACT, _INTENT])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    return AnalysisPipeline(maker, router)


async def test_integrate_note_end_to_end(maker, tmp_path):  # noqa: F811
    note_id = await make_note(maker, domain="general", body="Notes about Globex and its plans.")
    await ingest(maker, note_id, tmp_path)

    await _pipeline(maker).integrate_note({"note_id": note_id})

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
        note = (
            await session.execute(select(Note).where(Note.id == uuid.UUID(note_id)))
        ).scalar_one()

    assert len(ents) == 1  # the agent's new-mode resolution minted the entity
    assert len(facts) == 1  # the surface-attested fact committed through apply_intent
    assert facts[0].status == "active"
    assert note.integration_state == "integrated"  # the job flipped the lifecycle


async def test_integrate_note_passes_graph_context_to_the_agent(maker, tmp_path):  # noqa: F811
    # B3 wiring: an existing entity matching a mention must reach the integrate
    # call via the retrieved graph_context, so the agent is no longer blind. The
    # name is tagged to stay isolated in the shared module DB.
    tag = uuid.uuid4().hex[:8]
    name = f"Globex {tag}"
    note_id = await make_note(maker, domain="general", body=f"Notes about {name} and its plans.")
    await ingest(maker, note_id, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        ent = Entity(
            kind="Organization", canonical_name=name, status="confirmed", domain_code="general"
        )
        session.add(ent)
        await session.flush()
        ent_id = str(ent.id)

    extract = json.dumps(
        {
            "title": "Work",
            "tags": ["work", "career", "tech"],
            "mentions": [{"name": name, "kind": "Organization", "surface_text": name}],
            "facts": [],
            "temporal_tokens": [],
        }
    )
    fake = FakeLlmClient(responses=[extract, json.dumps({"resolutions": [], "facts": []})])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    await AnalysisPipeline(maker, router).integrate_note({"note_id": note_id})

    user_text = fake.calls[1]["user_text"]  # 0 = note.extract, 1 = integrate.note
    assert "Owner/author" in user_text  # the owner anchor is present
    assert ent_id in user_text  # the existing entity reached the agent (was "" before B3)


async def test_same_name_existing_resolution_is_overridden_to_ambiguous(maker, tmp_path):  # noqa: F811
    # Same-name guard (ANALYSIS.md "Same-name coexistence"): when the agent
    # confidently resolves a mention whose surface matches 2+ live entities to
    # mode:existing, the engine must NOT honor the guess — it withholds the override
    # so deterministic resolution routes the 2+-match ambiguity to an
    # ambiguous_mention card, and the fact attaches to NEITHER candidate. A per-run
    # LLM pick would flip run-to-run and silently mis-attribute (findings R3-02/R3-25).
    tag = uuid.uuid4().hex[:8]
    name = f"Max {tag}"
    async with scoped_session(maker, SYSTEM_CTX) as session:
        max_a = Entity(
            kind="Person", canonical_name=name, status="confirmed", domain_code="general"
        )
        max_b = Entity(
            kind="Person", canonical_name=name, status="confirmed", domain_code="general"
        )
        session.add_all([max_a, max_b])
        await session.flush()
        guessed_id = str(max_a.id)  # the id the agent (wrongly) commits to

    note_id = await make_note(
        maker, domain="general", body=f"{name} got promoted to staff engineer."
    )
    await ingest(maker, note_id, tmp_path)

    extract = json.dumps(
        {
            "title": "Promotion",
            "tags": ["work", "promotion"],
            "mentions": [{"name": name, "kind": "Person", "surface_text": name}],
            "facts": [
                {
                    "entity_ref": name,
                    "predicate": "occupation",
                    "qualifier": "",
                    "kind": "attribute",
                    "statement": f"{name} is a staff engineer.",
                    "value_json": {"title": "staff engineer"},
                    "assertion": "asserted",
                    "object_entity_ref": None,
                    "domain": "general",
                    "temporal": None,
                }
            ],
            "temporal_tokens": [],
        }
    )
    intent = json.dumps(
        {
            "resolutions": [{"mention_ref": name, "mode": "existing", "entity_id": guessed_id}],
            "facts": [
                {
                    "entity_ref": name,
                    "predicate": "occupation",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": f"{name} is a staff engineer.",
                    "value_json": {"title": "staff engineer"},
                    "self_confidence": 0.95,
                    "surface": name,
                }
            ],
        }
    )
    fake = FakeLlmClient(responses=[extract, intent])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    await AnalysisPipeline(maker, router).integrate_note({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as session:
        # The occupation fact attached to NEITHER Max — the guess was withheld.
        occ = (
            (
                await session.execute(
                    select(Fact).where(
                        Fact.predicate == "occupation",
                        Fact.entity_id.in_([max_a.id, max_b.id]),
                    )
                )
            )
            .scalars()
            .all()
        )
        cards = (
            (
                await session.execute(
                    select(ReviewItem).where(
                        ReviewItem.kind == "ambiguous_mention",
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert occ == []  # the agent's same-name guess never committed
    assert len(cards) == 1  # the collision surfaced as one ambiguous_mention card


async def test_integrate_note_missing_note_is_noop(maker, tmp_path):  # noqa: F811
    # A missing note must not raise (mirrors analyze_note's skip).
    await _pipeline(maker).integrate_note({"note_id": str(uuid.uuid4())})
