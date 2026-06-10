"""Hybrid search and the embed_note handler against real Postgres + pgvector.

Embedding vectors are deterministic fakes (the embed container never runs in
tests); dense-leg assertions plant hand-built vectors so cosine ordering is
provable, and every search path is exercised through the same RLS-scoped
sessions production uses.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import NoteEmbedder, vector_literal
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchResponse, SearchService
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))

DIMS = 384


def vec(*head: float) -> list[float]:
    v = [0.0] * DIMS
    for i, x in enumerate(head):
        v[i] = x
    return v


class StaticEmbed:
    """Deterministic embed fake: every text maps to the same fixed vector."""

    def __init__(self, vector: list[float] | None = None, fail: bool = False):
        self.vector = vector or vec(1.0)
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise ConnectionError("embed container down")
        return [self.vector for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_indexed_note(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path, *, domain: str, body: str
) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"se-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note.id})
    return note.id


async def plant_embedding(
    maker: async_sessionmaker[AsyncSession], note_id: str, vector: list[float]
) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "UPDATE app.chunks SET embedding = cast(:emb AS vector),"
                " embedding_model = 'planted' WHERE note_id = :nid"
            ),
            {"nid": note_id, "emb": vector_literal(vector)},
        )


async def search_as(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    q: str,
    qvec: list[float] | None = None,
    domain: str | None = None,
    fail_embed: bool = False,
) -> SearchResponse:
    service = SearchService(SqlSearchRepo(maker), StaticEmbed(vector=qvec, fail=fail_embed))
    return await service.search(ctx, q, domain, 20)


async def test_embed_note_handler_fills_only_null_embeddings(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    note_id = await make_indexed_note(
        maker, tmp_path, domain="general", body="first paragraph.\n\nsecond paragraph here."
    )
    client = StaticEmbed(vector=vec(0.5, 0.5))
    embedder = NoteEmbedder(maker, client, "fake-model")
    await embedder.embed_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT embedding IS NULL AS missing, embedding_model"
                    " FROM app.chunks WHERE note_id = :nid"
                ),
                {"nid": note_id},
            )
        ).all()
    assert rows and all(not r.missing and r.embedding_model == "fake-model" for r in rows)

    # Re-running finds nothing unembedded: no second client call.
    calls_before = len(client.calls)
    await embedder.embed_note({"note_id": note_id})
    assert len(client.calls) == calls_before


async def test_dense_ordering_follows_cosine_distance(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    closest = await make_indexed_note(maker, tmp_path, domain="general", body="alpha topic")
    middle = await make_indexed_note(maker, tmp_path, domain="general", body="beta topic")
    farthest = await make_indexed_note(maker, tmp_path, domain="general", body="gamma topic")
    await plant_embedding(maker, closest, vec(1.0, 0.0))
    await plant_embedding(maker, middle, vec(0.6, 0.8))
    await plant_embedding(maker, farthest, vec(0.0, 1.0))

    # 'zzzqq' matches nothing in FTS, so ordering is purely the dense leg.
    resp = await search_as(maker, OWNER, "zzzqq", qvec=vec(1.0, 0.0))
    assert not resp.degraded
    ordered = [r.note_id for r in resp.results if r.note_id in {closest, middle, farthest}]
    assert ordered == [closest, middle, farthest]
    assert all(r.match == "semantic" for r in resp.results)
    # Dense-only snippets are the chunk text, no <mark>s.
    assert resp.results[0].snippet == "alpha topic"


async def test_fts_relevance_headline_and_degraded_mode(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    hit_note = await make_indexed_note(
        maker, tmp_path, domain="general", body="pancake recipe with maple syrup drizzle"
    )
    await make_indexed_note(maker, tmp_path, domain="general", body="unrelated meeting agenda")

    resp = await search_as(maker, OWNER, "maple syrup", fail_embed=True)
    assert resp.degraded  # embed down -> FTS-only, never an error
    note_ids = [r.note_id for r in resp.results]
    assert hit_note in note_ids
    top = next(r for r in resp.results if r.note_id == hit_note)
    assert top.match == "keyword"
    assert "<mark>maple</mark>" in top.snippet and "<mark>syrup</mark>" in top.snippet
    assert top.body_preview.startswith("pancake recipe")
    assert top.attachment_count == 0
    assert top.source_kind == "note"


async def test_chunk_in_both_legs_is_labeled_both(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    note_id = await make_indexed_note(
        maker, tmp_path, domain="general", body="quarterly budget spreadsheet notes"
    )
    await plant_embedding(maker, note_id, vec(1.0))

    resp = await search_as(maker, OWNER, "budget spreadsheet", qvec=vec(1.0))
    top = next(r for r in resp.results if r.note_id == note_id)
    assert top.match == "both"
    assert "<mark>" in top.snippet  # FTS headline wins when both legs hit


async def test_search_respects_the_domain_firewall(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Rule 3 for search: a scoped token can never retrieve other-domain
    chunks through either leg."""
    health_note = await make_indexed_note(
        maker, tmp_path, domain="health", body="blood pressure 118 over 76 this morning"
    )
    general_note = await make_indexed_note(
        maker, tmp_path, domain="general", body="blood orange juice on the grocery list"
    )
    for nid in (health_note, general_note):
        await plant_embedding(maker, nid, vec(1.0))

    # The dense leg has no distance cutoff, so other general-domain test
    # notes may ride along — the proof is that health rows never do.
    scoped = await search_as(maker, GENERAL_ONLY, "blood pressure", qvec=vec(1.0))
    scoped_ids = {r.note_id for r in scoped.results}
    assert general_note in scoped_ids and health_note not in scoped_ids
    assert all(r.domain == "general" for r in scoped.results)

    unscoped = await search_as(maker, UNSCOPED, "blood pressure", qvec=vec(1.0))
    assert unscoped.results == []

    owner = await search_as(maker, OWNER, "blood pressure", qvec=vec(1.0))
    assert {health_note, general_note} <= {r.note_id for r in owner.results}

    # The domain param narrows further within the owner's full visibility.
    narrowed = await search_as(maker, OWNER, "blood pressure", qvec=vec(1.0), domain="health")
    assert health_note in {r.note_id for r in narrowed.results}
    assert all(r.domain == "health" for r in narrowed.results)


async def test_deleted_notes_are_excluded_from_both_legs(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    note_id = await make_indexed_note(
        maker, tmp_path, domain="general", body="ephemeral zebra sighting downtown"
    )
    await plant_embedding(maker, note_id, vec(1.0))
    # Soft-delete only — chunks deliberately left behind to prove the join
    # on deleted_at does the filtering even before delete's chunk hygiene.
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.notes SET deleted_at = now() WHERE id = :id"), {"id": note_id}
        )

    resp = await search_as(maker, OWNER, "zebra sighting", qvec=vec(1.0))
    assert all(r.note_id != note_id for r in resp.results)
