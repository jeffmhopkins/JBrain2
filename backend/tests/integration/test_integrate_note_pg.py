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
from jbrain.models.analysis import Entity, Fact
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


async def test_integrate_note_missing_note_is_noop(maker, tmp_path):  # noqa: F811
    # A missing note must not raise (mirrors analyze_note's skip).
    await _pipeline(maker).integrate_note({"note_id": str(uuid.uuid4())})
