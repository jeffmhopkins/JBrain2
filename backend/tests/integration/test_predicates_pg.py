"""The canonical_predicates index end to end against real Postgres (predicate
canonicalization Phase 2): the sync_predicates job seeds rows from the live
registry and fills embeddings, idempotently and model-aware, and the cosine
nearest-neighbour query returns the closest predicate. Embeddings are faked
(deterministic per descriptor) so the test never touches the TEI container.
"""

import hashlib
import random

import pytest
from sqlalchemy import text

import jbrain.queue as queue
from jbrain.analysis.predicates import nearest_predicates, registry_seed_rows
from jbrain.db.session import scoped_session
from jbrain.embed import PredicateEmbedder
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_MODEL = "test-embed-v1"


def _vec(t: str) -> list[float]:
    # Deterministic per text (stable across instances/processes), dense, near-unique:
    # identical descriptors embed identically (cosine 1), different ones diverge.
    rng = random.Random(int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big"))
    return [rng.uniform(-1, 1) for _ in range(384)]


class _FakeEmbed:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [_vec(t) for t in texts]


async def test_sync_predicates_seeds_and_embeds(maker):  # noqa: F811
    fake = _FakeEmbed()
    await PredicateEmbedder(maker, fake, _MODEL).sync_predicates({})

    expected = {r.canonical_name for r in registry_seed_rows()}
    async with scoped_session(maker, SYSTEM_CTX) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT canonical_name, embedding_model, origin,"
                    " embedding IS NOT NULL AS has_emb FROM app.canonical_predicates"
                )
            )
        ).all()
    assert {r.canonical_name for r in rows} == expected
    assert rows and all(
        r.embedding_model == _MODEL and r.has_emb and r.origin == "seed" for r in rows
    )


async def test_sync_predicates_is_idempotent(maker):  # noqa: F811
    emb = PredicateEmbedder(maker, _FakeEmbed(), _MODEL)
    await emb.sync_predicates({})
    fake = _FakeEmbed()
    # Re-run with a fresh client: every row is already at this model, so nothing
    # needs embedding and the client is never called.
    await PredicateEmbedder(maker, fake, _MODEL).sync_predicates({})
    assert fake.calls == 0


async def test_sync_predicates_reembeds_on_model_change(maker):  # noqa: F811
    await PredicateEmbedder(maker, _FakeEmbed(), _MODEL).sync_predicates({})
    fake = _FakeEmbed()
    await PredicateEmbedder(maker, fake, "test-embed-v2").sync_predicates({})
    assert fake.calls == 1  # the model changed -> every row re-embeds
    async with scoped_session(maker, SYSTEM_CTX) as session:
        models = set(
            (
                await session.execute(
                    text("SELECT DISTINCT embedding_model FROM app.canonical_predicates")
                )
            ).scalars()
        )
    assert models == {"test-embed-v2"}


async def test_sync_predicates_refreshes_a_drifted_descriptor(maker):  # noqa: F811
    # A row whose descriptor went stale (a registry edit) must be rewritten AND
    # re-embedded, not left matching the old vector.
    target = registry_seed_rows()[0]
    async with scoped_session(maker, SYSTEM_CTX) as session:
        # Force the stale state whether or not a prior test already seeded the row
        # (the module shares one DB).
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind, embedding, embedding_model)"
                " VALUES (:n, 'STALE DESCRIPTOR', :vs, :k, cast(:emb AS vector), :model)"
                " ON CONFLICT (canonical_name) DO UPDATE SET"
                " descriptor = 'STALE DESCRIPTOR',"
                " embedding = cast(:emb AS vector), embedding_model = :model"
            ),
            {
                "n": target.canonical_name,
                "vs": target.value_shape,
                "k": target.kind,
                "emb": "[" + ",".join(["0.0"] * 384) + "]",
                "model": _MODEL,
            },
        )

    await PredicateEmbedder(maker, _FakeEmbed(), _MODEL).sync_predicates({})

    async with scoped_session(maker, SYSTEM_CTX) as session:
        descriptor = (
            await session.execute(
                text("SELECT descriptor FROM app.canonical_predicates WHERE canonical_name = :n"),
                {"n": target.canonical_name},
            )
        ).scalar_one()
        near = await nearest_predicates(session, _vec(target.descriptor), k=1)
    assert descriptor == target.descriptor  # refreshed from the registry
    # re-embedded against the NEW descriptor (the zero vector would not match it)
    assert near[0][0] == target.canonical_name and near[0][1] > 0.99


async def test_nearest_predicates_returns_closest(maker):  # noqa: F811
    await PredicateEmbedder(maker, _FakeEmbed(), _MODEL).sync_predicates({})
    target = registry_seed_rows()[0]
    async with scoped_session(maker, SYSTEM_CTX) as session:
        near = await nearest_predicates(session, _vec(target.descriptor), k=1)
    assert near and near[0][0] == target.canonical_name and near[0][1] > 0.99


async def test_backfill_sync_predicates_enqueues_one(maker):  # noqa: F811
    first = await queue.backfill_sync_predicates(maker, SYSTEM_CTX)
    second = await queue.backfill_sync_predicates(maker, SYSTEM_CTX)
    assert first == 1 and second == 0  # one job scheduled, then the dup guard holds
