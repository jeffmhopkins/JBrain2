"""GET /api/entities (the browse list) against real Postgres: ordering,
substring + kind filters, merged tombstones excluded, and RLS via the
session context (CLAUDE.md rule 3).

The module database is shared and the seed fixture runs per test, so every
seed is tagged: names, aliases, and kinds carry a unique suffix, and list
assertions filter down to that seed's ids before comparing order."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
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
from jbrain.models.analysis import Entity, EntityAlias, EntityMention, Fact
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


def at(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=UTC)


def make_fact(entity_id: uuid.UUID, note_id: uuid.UUID, **over: Any) -> Fact:
    defaults: dict[str, Any] = {
        "entity_id": entity_id,
        "predicate": f"p-{uuid.uuid4().hex[:8]}",
        "qualifier": "",
        "kind": "state",
        "statement": "seeded",
        "assertion": "asserted",
        "status": "active",
        "valid_from": at(1),
        "reported_at": at(1),
        "note_id": note_id,
        "extractor": "test",
        "prompt_version": "test",
        "domain_code": "general",
    }
    defaults.update(over)
    return Fact(**defaults)


def make_mention(entity_id: uuid.UUID, chunk_id: Any, note_id: uuid.UUID) -> EntityMention:
    return EntityMention(
        entity_id=entity_id,
        chunk_id=chunk_id,
        note_id=note_id,
        surface_text="seed",
        char_start=0,
        char_end=4,
        link_method="exact_alias",
        domain_code="general",
    )


@pytest.fixture
async def seeded(maker: async_sessionmaker[AsyncSession], tmp_path: Any) -> dict[str, str]:
    """Four visible entities plus a merged tombstone and a health-walled one.

    Expected ordering within the seed: Helio (seen 6/11) > Alma (6/10) >
    Beta (6/9) > Carl (no facts -> null last_seen, sorts last).
    """
    tag = uuid.uuid4().hex[:8]
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"el-{tag}", domain="general", destination=None, body="Alma note"
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note.id})
    note_id = uuid.UUID(note.id)
    async with scoped_session(maker, OWNER) as session:
        chunk_id = (
            await session.execute(
                text("SELECT id FROM app.chunks WHERE note_id = :nid LIMIT 1"), {"nid": note.id}
            )
        ).scalar_one()
        alma = Entity(
            kind=f"Person-{tag}",
            canonical_name=f"Alma {tag}",
            status="confirmed",
            domain_code="general",
        )
        beta = Entity(
            kind=f"Org-{tag}",
            canonical_name=f"Betamill {tag}",
            status="provisional",
            domain_code="general",
        )
        carl = Entity(
            kind=f"Person-{tag}",
            canonical_name=f"Carl {tag}",
            status="provisional",
            domain_code="general",
        )
        helio = Entity(
            kind=f"Org-{tag}",
            canonical_name=f"Helio {tag}",
            status="confirmed",
            domain_code="health",
        )
        session.add_all([alma, beta, carl, helio])
        await session.flush()
        dup = Entity(
            kind=f"Person-{tag}",
            canonical_name=f"Alma dup {tag}",
            status="merged",
            merged_into_id=alma.id,
            domain_code="general",
        )
        session.add(dup)
        session.add_all(
            [
                EntityAlias(
                    entity_id=beta.id,
                    alias=f"the mill {tag}",
                    alias_norm=f"the mill {tag}",
                    domain_code="general",
                ),
                EntityAlias(
                    entity_id=carl.id,
                    alias=f"charlie-{tag}",
                    alias_norm=f"charlie-{tag}",
                    domain_code="general",
                ),
            ]
        )
        session.add_all(
            [
                # Alma: 2 live edges; the retracted/superseded ones still
                # count toward last_seen but never toward fact_count.
                make_fact(alma.id, note_id, status="active", reported_at=at(10)),
                make_fact(alma.id, note_id, status="pending_review", reported_at=at(8)),
                make_fact(alma.id, note_id, status="retracted", reported_at=at(4)),
                make_fact(alma.id, note_id, status="superseded", reported_at=at(1)),
                make_fact(beta.id, note_id, reported_at=at(9)),
                make_fact(helio.id, note_id, reported_at=at(11), domain_code="health"),
            ]
        )
        session.add_all(
            [
                make_mention(alma.id, chunk_id, note_id),
                make_mention(alma.id, chunk_id, note_id),
                make_mention(carl.id, chunk_id, note_id),
            ]
        )
    return {
        "tag": tag,
        "alma": str(alma.id),
        "beta": str(beta.id),
        "carl": str(carl.id),
        "helio": str(helio.id),
        "dup": str(dup.id),
    }


def mine(items: list[dict[str, Any]], seeded: dict[str, str]) -> list[str]:
    """This seed's ids in list order — other tests' rows fall away."""
    ours = {seeded[k] for k in ("alma", "beta", "carl", "helio", "dup")}
    return [i["id"] for i in items if i["id"] in ours]


async def test_list_orders_counts_and_excludes_merged(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    items = await SqlAnalysisRepo(maker).list_entities(OWNER)
    assert mine(items, seeded) == [seeded["helio"], seeded["alma"], seeded["beta"], seeded["carl"]]
    by_id = {i["id"]: i for i in items}
    alma = by_id[seeded["alma"]]
    assert alma["canonical_name"] == f"Alma {seeded['tag']}"
    assert alma["kind"] == f"Person-{seeded['tag']}" and alma["status"] == "confirmed"
    assert alma["fact_count"] == 2  # active + pending_review only
    assert alma["mention_count"] == 2
    assert alma["last_seen"] == at(10)
    assert alma["aliases"] == []  # no aliases → empty list, never null
    carl = by_id[seeded["carl"]]
    assert carl["fact_count"] == 0 and carl["mention_count"] == 1
    assert carl["last_seen"] is None  # null-safe: sorts last, never errors
    assert carl["aliases"] == [f"charlie-{seeded['tag']}"]  # surfaced for prose linking


async def test_list_q_matches_names_and_aliases_literally(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    tag = seeded["tag"]
    hits = await repo.list_entities(OWNER, q=f"alma {tag}")
    assert [i["id"] for i in hits] == [seeded["alma"]]  # the merged dup stays out
    # Case-insensitive; a name+alias double-hit stays a single row.
    assert [i["id"] for i in await repo.list_entities(OWNER, q=f"MILL {tag}")] == [seeded["beta"]]
    # Alias-only hit.
    assert [i["id"] for i in await repo.list_entities(OWNER, q=f"charlie-{tag}")] == [
        seeded["carl"]
    ]
    # LIKE wildcards in the query are literals, not patterns.
    assert await repo.list_entities(OWNER, q=f"alma_{tag}") == []
    assert await repo.list_entities(OWNER, q=f"alma%{tag}") == []


async def test_list_q_tokens_match_across_different_aliases(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A multi-word query lands even when its words live on different aliases:
    "Jeff Hopkins" finds an entity aliased "Jeff" and "Jeffrey Mark Hopkins",
    which the old contiguous-substring match missed. AND across tokens still
    keeps precision — a word with no home (Smith) excludes the row."""
    tag = uuid.uuid4().hex[:8]
    repo = SqlAnalysisRepo(maker)
    async with scoped_session(maker, OWNER) as session:
        me = Entity(
            kind=f"Person-{tag}",
            canonical_name=f"Me {tag}",
            status="confirmed",
            domain_code="general",
        )
        session.add(me)
        await session.flush()
        session.add_all(
            [
                EntityAlias(
                    entity_id=me.id,
                    alias=f"Jeff {tag}",
                    alias_norm=f"jeff {tag}",
                    domain_code="general",
                ),
                EntityAlias(
                    entity_id=me.id,
                    alias=f"Jeffrey Mark Hopkins {tag}",
                    alias_norm=f"jeffrey mark hopkins {tag}",
                    domain_code="general",
                ),
            ]
        )
    mid = str(me.id)

    hit = await repo.list_entities(OWNER, q=f"Jeff Hopkins {tag}")
    assert [i["id"] for i in hit if i["id"] == mid] == [mid]
    # Order-independent, and case-insensitive.
    assert mid in {i["id"] for i in await repo.list_entities(OWNER, q=f"hopkins jeff {tag}")}
    # Precision: a stray token with no name/alias home drops the row.
    assert mid not in {i["id"] for i in await repo.list_entities(OWNER, q=f"Jeff Smith {tag}")}


async def test_list_kind_filter_and_limit(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    people = await repo.list_entities(OWNER, kind=f"Person-{seeded['tag']}")
    assert [i["id"] for i in people] == [seeded["alma"], seeded["carl"]]
    assert len(await repo.list_entities(OWNER, limit=1)) == 1


async def test_list_is_rls_scoped(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    ids = {i["id"] for i in await repo.list_entities(GENERAL_ONLY)}
    assert seeded["helio"] not in ids  # the health firewall holds
    assert {seeded["alma"], seeded["beta"], seeded["carl"]} <= ids
    # An unscoped principal sees no entities at all: counts can't leak.
    assert await repo.list_entities(UNSCOPED) == []


async def test_entities_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
    seeded: dict[str, str],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "it"})
        assert login.status_code == 204
        items = client.get("/api/entities").json()["items"]
        assert mine(items, seeded) == [
            seeded["helio"],
            seeded["alma"],
            seeded["beta"],
            seeded["carl"],
        ]
        # The wire shape is the frozen frontend contract.
        assert set(items[0]) == {
            "id",
            "kind",
            "canonical_name",
            "status",
            "domain",
            "aliases",
            "fact_count",
            "mention_count",
            "last_seen",
        }
        filtered = client.get(
            "/api/entities",
            params={"q": f"alma {seeded['tag']}", "kind": f"Person-{seeded['tag']}"},
        ).json()
        assert [i["id"] for i in filtered["items"]] == [seeded["alma"]]
        assert client.get("/api/entities", params={"kind": "Spaceship"}).json()["items"] == []
