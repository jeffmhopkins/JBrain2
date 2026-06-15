"""Embedding predicate canonicalization end to end (Phase 3a) against real
Postgres, with the LLM + embedder faked. Proves: a STRONG match rewrites an
unknown predicate to its canonical BEFORE the arbiter keys the fact (the
committed graph address collapses); a cold match leaves the raw predicate and
files a new_predicate review card; and the whole pass is inert when the
predicate_canonicalization setting is off (the default).
"""

import hashlib
import json
import random

import pytest
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.predicates import raw_descriptor
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import PREDICATE_CANON_KEY, SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_MODEL = "test-embed-v1"
_STMT = "Pat is married to Dana."


def _vec(t: str) -> list[float]:
    rng = random.Random(int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big"))
    return [rng.uniform(-1, 1) for _ in range(384)]


class _FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_vec(t) for t in texts]


def _router(predicate: str) -> LlmRouter:
    extract = json.dumps(
        {
            "title": "t",
            "tags": [],
            "mentions": [
                {"name": "Pat", "kind": "Person", "surface_text": "Pat"},
                {"name": "Dana", "kind": "Person", "surface_text": "Dana"},
            ],
            "facts": [],
            "temporal_tokens": [],
        }
    )
    intent = json.dumps(
        {
            "resolutions": [
                {"mention_ref": "m1", "mode": "new", "new_kind": "Person", "new_name": "Pat"},
                {"mention_ref": "m2", "mode": "new", "new_kind": "Person", "new_name": "Dana"},
            ],
            "facts": [
                {
                    "entity_ref": "m1",
                    "predicate": predicate,
                    "kind": "relationship",
                    "assertion": "asserted",
                    "statement": _STMT,
                    "object_entity_ref": "m2",
                    "self_confidence": 0.95,
                    "surface": "married",
                }
            ],
        }
    )
    return LlmRouter(
        {"xai": FakeLlmClient(responses=[extract, intent])},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )


def _pipeline(maker, predicate: str) -> AnalysisPipeline:  # noqa: F811
    return AnalysisPipeline(
        maker,
        _router(predicate),
        embedder=_FakeEmbed(),
        embed_model=_MODEL,
        settings=SqlSettingsStore(maker),
    )


async def _set_flag(maker, value: bool) -> None:  # noqa: F811
    await SqlSettingsStore(maker).upsert(SYSTEM_CTX, PREDICATE_CANON_KEY, value)


async def _seed_canonical(maker, name: str, embedding_text: str) -> None:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind, embedding, embedding_model)"
                " VALUES (:n, 'd', 'ref', 'relationship', cast(:emb AS vector), :model)"
                " ON CONFLICT (canonical_name) DO UPDATE SET"
                " embedding = cast(:emb AS vector), embedding_model = :model"
            ),
            {
                "n": name,
                "emb": "[" + ",".join(map(str, _vec(embedding_text))) + "]",
                "model": _MODEL,
            },
        )


async def _committed_predicates(maker, note_id: str) -> set[str]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return set(
            (
                await session.execute(
                    text("SELECT predicate FROM app.facts WHERE note_id = :nid"),
                    {"nid": note_id},
                )
            ).scalars()
        )


async def test_strong_match_rewrites_the_committed_predicate(maker, tmp_path):  # noqa: F811
    pred = "isHitchedTo"
    # Seed 'spouse' with the exact vector the incoming predicate's descriptor
    # embeds to (cosine 1 -> STRONG).
    await _seed_canonical(maker, "spouse", raw_descriptor(pred, _STMT, "relationship"))
    await _set_flag(maker, True)
    note_id = await make_note(maker, domain="general", body=_STMT)
    await ingest(maker, note_id, tmp_path)

    await _pipeline(maker, pred).integrate_note({"note_id": note_id})

    predicates = await _committed_predicates(maker, note_id)
    assert "spouse" in predicates  # rewritten before keying
    assert pred.lower() not in {p.lower() for p in predicates}


async def test_cold_match_keeps_raw_and_files_a_card(maker, tmp_path):  # noqa: F811
    pred = "isBondedWith"  # distinct so the global card dedup doesn't collide
    # No seeded canonical matches this descriptor (any prior seed embeds to a
    # ~orthogonal vector, cosine << WEAK) -> cold -> a new_predicate card.
    await _set_flag(maker, True)
    note_id = await make_note(maker, domain="general", body=_STMT)
    await ingest(maker, note_id, tmp_path)

    await _pipeline(maker, pred).integrate_note({"note_id": note_id})

    assert pred in await _committed_predicates(maker, note_id)  # raw, never rejected
    async with scoped_session(maker, SYSTEM_CTX) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, payload FROM app.review_items"
                    " WHERE kind = 'new_predicate' AND payload->>'predicate' = :p"
                ),
                {"p": pred},
            )
        ).one()
    assert row.payload["predicate"] == pred and row.payload["note_id"] == note_id
    # The card carries the triggering edge (mention_refs resolved to the agent's
    # names) so the UI can preview subject.<canonical> -> value, plus the ranked
    # candidate list it renders instead of raw cosine numbers.
    assert row.payload["subject"] == "Pat" and row.payload["value"] == "Dana"
    assert isinstance(row.payload["suggestions"], list)
    # The card is dismissable in 3a (accept/map land in 3b): reject must NOT
    # raise UnknownAction — it leaves the fact under its raw name.
    resolved = await SqlAnalysisRepo(maker).resolve_review(OWNER, str(row.id), "reject", {})
    assert resolved is not None and resolved["status"] != "open"


async def test_canonicalization_is_inert_when_the_setting_is_off(maker, tmp_path):  # noqa: F811
    pred = "isPairedWith"
    await _seed_canonical(maker, "spouse", raw_descriptor(pred, _STMT, "relationship"))
    await _set_flag(maker, False)  # default; explicit here against the shared DB
    note_id = await make_note(maker, domain="general", body=_STMT)
    await ingest(maker, note_id, tmp_path)

    await _pipeline(maker, pred).integrate_note({"note_id": note_id})

    assert pred in await _committed_predicates(maker, note_id)  # no rewrite
    async with scoped_session(maker, SYSTEM_CTX) as session:
        cards = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.review_items"
                    " WHERE kind = 'new_predicate' AND payload->>'predicate' = :p"
                ),
                {"p": pred},
            )
        ).scalar()
    assert cards == 0  # no card filed
