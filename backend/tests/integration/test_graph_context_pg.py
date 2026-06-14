"""build_graph_context against real Postgres: the integrate agent's view of the
existing graph. Exercises the three candidate signals (exact/alias name, owner
ego-graph) and — the security-critical case (CLAUDE.md rule 3, a new read path) —
the firewall: the job runs under the all-seeing SYSTEM_CTX, so an EXPLICIT domain
filter (not RLS) must keep a restricted-domain entity or fact out of a general
note's context. The embedder/vector layer is left None here for determinism; it
reuses _embedding_candidates, already covered by the resolution tests.

The module database is shared, so the seed tags every name/kind with a unique
suffix and assertions key off this seed's ids.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.extraction import ExtractedMention
from jbrain.analysis.graph_context import build_graph_context
from jbrain.db.session import scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.models.analysis import Entity, Fact
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _rel(src: uuid.UUID, dst: uuid.UUID, note_id: uuid.UUID, predicate: str, domain: str) -> Fact:
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
        reported_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        note_id=note_id,
        extractor="test",
        prompt_version="test",
        domain_code=domain,
    )


def _attr(eid: uuid.UUID, note_id: uuid.UUID, predicate: str, value: str, domain: str) -> Fact:
    return Fact(
        entity_id=eid,
        object_entity_id=None,
        predicate=predicate,
        qualifier="",
        kind="state",
        statement=f"{predicate} is {value}",
        value_json={"value": value},
        assertion="asserted",
        status="active",
        valid_from=None,
        reported_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        note_id=note_id,
        extractor="test",
        prompt_version="test",
        domain_code=domain,
    )


@pytest.fixture
async def seeded(maker: async_sessionmaker[AsyncSession], tmp_path: Any) -> dict[str, str]:
    """An owner-centred graph with a health pocket:

    me --spouse--> wife (general)        wife.gender=female (general), wife.dx (HEALTH)
    me --worksFor--> employer (general)
    me --seenAt--> clinic (HEALTH)       (clinic reachable only via a health edge)
    okafor (general, unconnected)        — exact-name match target
    """
    tag = uuid.uuid4().hex[:8]
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"gc-{tag}", domain="general", destination=None, body="seed"
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note.id})
    note_id = uuid.UUID(note.id)
    async with scoped_session(maker, OWNER) as session:

        def ent(name: str, kind: str, domain: str) -> Entity:
            return Entity(
                kind=f"{kind}-{tag}",
                canonical_name=f"{name} {tag}",
                status="confirmed",
                domain_code=domain,
            )

        me = ent("Me", "Person", "general")
        wife = ent("Wife", "Person", "general")
        employer = ent("Acme", "Organization", "general")
        clinic = ent("Clinic", "Organization", "health")
        okafor = ent("Dr. Okafor", "Person", "general")
        # A health-domain namesake of the general okafor: a general note mentioning
        # "Dr. Okafor" must surface the general entity and NEVER the health one
        # (the entity-row firewall, not just the fact/edge filters).
        okafor_health = ent("Dr. Okafor", "Person", "health")
        session.add_all([me, wife, employer, clinic, okafor, okafor_health])
        await session.flush()
        session.add_all(
            [
                _rel(me.id, wife.id, note_id, "spouse", "general"),
                _rel(me.id, employer.id, note_id, "worksFor", "general"),
                _rel(me.id, clinic.id, note_id, "seenAt", "health"),
                _attr(wife.id, note_id, "gender", "female", "general"),
                _attr(wife.id, note_id, "diagnosis", "hypertension", "health"),
            ]
        )
    return {
        "tag": tag,
        "me": str(me.id),
        "wife": str(wife.id),
        "employer": str(employer.id),
        "clinic": str(clinic.id),
        "okafor": str(okafor.id),
        "okafor_health": str(okafor_health.id),
    }


def _mention(name: str, kind: str = "Person") -> ExtractedMention:
    return ExtractedMention(name=name, kind=kind, surface_text=name)


async def _context(
    maker: async_sessionmaker[AsyncSession],
    seeded: dict[str, str],
    mentions: list[ExtractedMention],
    *,
    domain: str = "general",
) -> str:
    async with scoped_session(maker, OWNER) as session:
        return await build_graph_context(
            session,
            owner_id=uuid.UUID(seeded["me"]),
            mentions=mentions,
            note_domain=domain,
            embedder=None,
            embed_model="",
        )


async def test_owner_is_rendered_first_with_its_id(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    out = await _context(maker, seeded, [])
    assert out.startswith(f"Owner/author: entity id '{seeded['me']}' name 'Me {seeded['tag']}'")


async def test_exact_name_mention_surfaces_entity_with_id(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    out = await _context(maker, seeded, [_mention(f"Dr. Okafor {seeded['tag']}")])
    assert f"- id '{seeded['okafor']}' name 'Dr. Okafor {seeded['tag']}'" in out


async def test_owner_ego_graph_surfaces_relations_without_a_name_match(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    # No mentions at all: spouse + employer still appear via owner proximity.
    out = await _context(maker, seeded, [])
    assert seeded["wife"] in out and seeded["employer"] in out


async def test_firewall_excludes_health_entity_and_facts_from_a_general_note(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    # All-seeing OWNER session (like SYSTEM_CTX) — only the explicit domain filter
    # keeps health out. The clinic (reachable only via a health edge) is absent,
    # the owner's health seenAt edge is absent, and on the surfaced wife her
    # general gender shows while her health diagnosis does not.
    out = await _context(maker, seeded, [_mention(f"Wife {seeded['tag']}")])
    assert seeded["clinic"] not in out
    assert "seenAt" not in out
    assert seeded["wife"] in out
    assert "gender -> female" in out
    assert "diagnosis" not in out and "hypertension" not in out


async def test_firewall_excludes_a_health_entity_whose_name_matches_a_general_mention(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    # The exact-name path: a general note mentions "Dr. Okafor"; both a general
    # and a health entity carry that name. Only the general one may surface —
    # the entity-row domain filter must drop the health namesake.
    out = await _context(maker, seeded, [_mention(f"Dr. Okafor {seeded['tag']}")])
    assert seeded["okafor"] in out
    assert seeded["okafor_health"] not in out


async def test_no_embedder_still_returns_exact_and_graph_candidates(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    # embedder=None (the vector layer is skipped): exact-name + ego-graph alone
    # must still produce a non-empty, id-bearing context.
    out = await _context(maker, seeded, [_mention(f"Dr. Okafor {seeded['tag']}")])
    assert "Known entities:" in out
    assert seeded["okafor"] in out  # exact name
    assert seeded["wife"] in out  # ego-graph
