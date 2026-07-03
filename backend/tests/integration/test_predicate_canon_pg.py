"""The two-tier predicate pipeline end to end (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md
§1) against real Postgres, with the LLM faked. Proves: a durable predicate_alias
rewrites an unknown predicate to its canonical BEFORE the arbiter keys the fact
(no embedder required); an unaliased long-tail predicate commits raw with NO
embed round-trip and NO new_predicate card (the retired noise machinery); the
alias collapse ignores the predicate_canonicalization setting (repurposed — it
now gates only the held-fact suggestion picker); and the picker's on-demand
suggestion source still works.
"""

import hashlib
import json
import random
import uuid

import pytest
import structlog.testing
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.predicates import raw_descriptor, record_predicate_alias
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
    def __init__(self) -> None:
        self.texts: list[str] = []  # lets tests pin what was (never) embedded

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
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


def _pipeline(maker, predicate: str, *, embedder: _FakeEmbed | None = None) -> AnalysisPipeline:  # noqa: F811
    return AnalysisPipeline(
        maker,
        _router(predicate),
        embedder=embedder,
        embed_model=_MODEL if embedder else "",
        settings=SqlSettingsStore(maker),
    )


async def _set_flag(maker, value: bool) -> None:  # noqa: F811
    await SqlSettingsStore(maker).upsert(SYSTEM_CTX, PREDICATE_CANON_KEY, value)


async def _seed_alias(maker, raw: str, canonical: str) -> None:  # noqa: F811
    """A canonical row (the aliases FK target — no embedding needed) plus the
    durable raw→canonical alias a past owner resolution would have written."""
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind)"
                " VALUES (:n, 'd', 'ref', 'relationship')"
                " ON CONFLICT (canonical_name) DO NOTHING"
            ),
            {"n": canonical},
        )
        await record_predicate_alias(session, raw, canonical)


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


async def _new_predicate_cards(maker, predicate: str) -> int:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.review_items"
                    " WHERE kind = 'new_predicate' AND payload->>'predicate' = :p"
                ),
                {"p": predicate},
            )
        ).scalar_one()


async def test_durable_alias_rewrites_the_committed_predicate(maker, tmp_path):  # noqa: F811
    # A past owner map_to_existing decision collapses the drift spelling — with
    # NO embedder configured, proving the collapse is a pure aliases lookup.
    pred = "isHitchedTo"
    await _seed_alias(maker, pred, "spouse")
    note_id = await make_note(maker, domain="general", body=_STMT)
    await ingest(maker, note_id, tmp_path)

    await _pipeline(maker, pred).integrate_note({"note_id": note_id})

    predicates = await _committed_predicates(maker, note_id)
    assert "spouse" in predicates  # rewritten before keying
    assert pred.lower() not in {p.lower() for p in predicates}


async def test_longtail_predicate_commits_raw_with_no_card(maker, tmp_path):  # noqa: F811
    # The Wave-1 inversion: the exact configuration that used to file a
    # new_predicate card (embedder live, setting ON) now commits the raw
    # predicate with no card and logs predicate.longtail_kept instead.
    pred = "isBondedWith"
    await _set_flag(maker, True)
    note_id = await make_note(maker, domain="general", body=_STMT)
    await ingest(maker, note_id, tmp_path)
    embedder = _FakeEmbed()

    with structlog.testing.capture_logs() as logs:
        await _pipeline(maker, pred, embedder=embedder).integrate_note({"note_id": note_id})

    assert pred in await _committed_predicates(maker, note_id)  # raw, never rejected
    # The live embedder still serves graph-context entity candidates, but the
    # long-tail predicate itself never took an embed round-trip: its descriptor
    # (humanized "is bonded with" + statement) never reached embed().
    assert not any("bonded" in t.lower() for t in embedder.texts)
    assert await _new_predicate_cards(maker, pred) == 0
    kept = [e for e in logs if e["event"] == "predicate.longtail_kept"]
    assert kept and kept[0]["predicate"] == pred and kept[0]["note_id"] == note_id


async def test_alias_collapse_ignores_the_repurposed_setting(maker, tmp_path):  # noqa: F811
    # predicate_canonicalization now gates only the held-fact suggestion picker;
    # the durable collapse honors past owner decisions regardless of the flag.
    pred = "isPairedWith"
    await _seed_alias(maker, pred, "spouse")
    await _set_flag(maker, False)
    note_id = await make_note(maker, domain="general", body=_STMT)
    await ingest(maker, note_id, tmp_path)

    await _pipeline(maker, pred).integrate_note({"note_id": note_id})

    predicates = await _committed_predicates(maker, note_id)
    assert "spouse" in predicates
    assert pred.lower() not in {p.lower() for p in predicates}


async def test_predicate_suggestions_for_a_card(maker):  # noqa: F811
    # The on-demand picker source: nearest canonicals for a held card's predicate,
    # computed live so cards filed before the picker existed still get them. Seed
    # 'spouse' at the predicate descriptor's exact vector (cosine 1 → top).
    pred = "isFondOf"
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
                "n": "spouse",
                "emb": "[" + ",".join(map(str, _vec(raw_descriptor(pred, "x", None)))) + "]",
                "model": _MODEL,
            },
        )
    iid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                " VALUES (:id, 'low_confidence_inference', cast(:p AS jsonb), 'general')"
            ),
            {"id": iid, "p": json.dumps({"predicate": pred, "statement": "x"})},
        )
    repo = SqlAnalysisRepo(maker)

    out = await repo.predicate_suggestions(OWNER, iid, embedder=_FakeEmbed())
    assert out is not None and any(s["name"] == "spouse" for s in out)
    # A missing item is None (a 404 upstream), distinct from an empty list.
    missing = await repo.predicate_suggestions(OWNER, str(uuid.uuid4()), embedder=_FakeEmbed())
    assert missing is None
