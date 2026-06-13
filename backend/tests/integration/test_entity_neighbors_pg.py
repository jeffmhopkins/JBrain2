"""GET /api/entities/{id}/neighbors (the graph-view ego subgraph) against real
Postgres: depth, directed edges, merged tombstones excluded, and the RLS
firewall (CLAUDE.md rule 3 — every new read path needs an isolation test).

The module database is shared, so the seed tags every name/kind with a unique
suffix and assertions filter to this seed's ids before comparing.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.main import create_app
from jbrain.models.analysis import Entity, Fact
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def rel(src: uuid.UUID, dst: uuid.UUID, note_id: uuid.UUID, predicate: str, domain: str) -> Fact:
    """A directed relationship edge src --predicate--> dst."""
    return Fact(
        entity_id=src,
        object_entity_id=dst,
        predicate=predicate,
        qualifier="",
        kind="relationship",
        statement=f"{predicate} edge",
        assertion="asserted",
        status="active",
        valid_from=None,
        reported_at=None,
        note_id=note_id,
        extractor="test",
        prompt_version="test",
        domain_code=domain,
    )


@pytest.fixture
async def seeded(maker: async_sessionmaker[AsyncSession], tmp_path: Any) -> dict[str, str]:
    """An ego network around "Me":

    me --spouse--> wife          (general, 1 hop)
    me --worksFor--> employer    (general, 1 hop)
    me --seenAt--> clinic        (HEALTH, 1 hop)
    wife --sibling--> distant    (general, 2 hops)
    clinic --prescribes--> med   (HEALTH, 2 hops)
    + a merged tombstone me points at, which must never surface.
    """
    tag = uuid.uuid4().hex[:8]
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"nb-{tag}", domain="general", destination=None, body="seed"
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note.id})
    note_id = uuid.UUID(note.id)
    async with scoped_session(maker, OWNER) as session:

        def ent(name: str, kind: str, domain: str, status: str = "confirmed") -> Entity:
            return Entity(
                kind=f"{kind}-{tag}",
                canonical_name=f"{name} {tag}",
                status=status,
                domain_code=domain,
            )

        me = ent("Me", "Person", "general")
        wife = ent("Wife", "Person", "general")
        employer = ent("Acme", "Organization", "general")
        clinic = ent("Clinic", "Organization", "health")
        distant = ent("Sibling", "Person", "general")
        med = ent("Med", "Drug", "health")
        session.add_all([me, wife, employer, clinic, distant, med])
        await session.flush()
        gone = ent("Ghost", "Person", "general", status="merged")
        gone.merged_into_id = wife.id
        session.add(gone)
        await session.flush()
        session.add_all(
            [
                rel(me.id, wife.id, note_id, "spouse", "general"),
                rel(me.id, employer.id, note_id, "worksFor", "general"),
                rel(me.id, clinic.id, note_id, "seenAt", "health"),
                rel(wife.id, distant.id, note_id, "sibling", "general"),
                rel(clinic.id, med.id, note_id, "prescribes", "health"),
                rel(me.id, gone.id, note_id, "knows", "general"),
            ]
        )
    return {
        "tag": tag,
        "me": str(me.id),
        "wife": str(wife.id),
        "employer": str(employer.id),
        "clinic": str(clinic.id),
        "distant": str(distant.id),
        "med": str(med.id),
        "gone": str(gone.id),
    }


def node_ids(graph: dict[str, Any]) -> set[str]:
    return {n["id"] for n in graph["nodes"]}


def edge_set(graph: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["predicate"]) for e in graph["edges"]}


async def test_depth_one_is_focal_plus_direct_neighbours(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    graph = await SqlAnalysisRepo(maker).ego_graph(OWNER, seeded["me"], depth=1)
    assert graph is not None
    assert graph["root"] == seeded["me"] and graph["depth"] == 1
    assert node_ids(graph) == {seeded["me"], seeded["wife"], seeded["employer"], seeded["clinic"]}
    # Directed edges, deduped, the merged tombstone excluded.
    assert (seeded["me"], seeded["wife"], "spouse") in edge_set(graph)
    assert (seeded["me"], seeded["clinic"], "seenAt") in edge_set(graph)
    assert seeded["gone"] not in node_ids(graph)
    assert seeded["med"] not in node_ids(graph)  # 2 hops away, not at depth 1


async def test_depth_two_reaches_second_hop(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    graph = await SqlAnalysisRepo(maker).ego_graph(OWNER, seeded["me"], depth=2)
    assert graph is not None
    assert {seeded["distant"], seeded["med"]} <= node_ids(graph)
    assert (seeded["wife"], seeded["distant"], "sibling") in edge_set(graph)
    assert (seeded["clinic"], seeded["med"], "prescribes") in edge_set(graph)


async def test_neighbors_are_rls_scoped(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    graph = await repo.ego_graph(GENERAL_ONLY, seeded["me"], depth=2)
    assert graph is not None
    ids = node_ids(graph)
    # The health firewall holds: the clinic, the edge to it, and the drug
    # reachable only through it all vanish for a general-only session.
    assert seeded["clinic"] not in ids
    assert seeded["med"] not in ids
    assert not any(e["target"] == seeded["clinic"] for e in graph["edges"])
    # General neighbours still resolve.
    assert {seeded["wife"], seeded["employer"], seeded["distant"]} <= ids
    # An unscoped principal can't even see the focal entity.
    assert await repo.ego_graph(UNSCOPED, seeded["me"], depth=1) is None


async def test_unknown_or_malformed_id(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    assert await repo.ego_graph(OWNER, "not-a-uuid", depth=1) is None
    assert await repo.ego_graph(OWNER, str(uuid.uuid4()), depth=1) is None


async def test_neighbors_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
    seeded: dict[str, str],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "it"})
        assert login.status_code == 204
        graph = client.get(f"/api/entities/{seeded['me']}/neighbors", params={"depth": 2}).json()
        assert graph["root"] == seeded["me"] and graph["depth"] == 2
        assert {n["id"] for n in graph["nodes"]} >= {seeded["wife"], seeded["med"]}
        # Frozen wire shape for the frontend.
        assert set(graph) == {"root", "depth", "nodes", "edges"}
        assert set(graph["nodes"][0]) == {"id", "kind", "canonical_name", "status", "domain"}
        assert set(graph["edges"][0]) == {"source", "target", "predicate"}
        # depth is clamped to [1, 2] by the query validator.
        too_deep = client.get(f"/api/entities/{seeded['me']}/neighbors", params={"depth": 9})
        assert too_deep.status_code == 422  # depth clamped to [1, 2] by the validator
        assert client.get(f"/api/entities/{uuid.uuid4()}/neighbors").status_code == 404
