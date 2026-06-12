"""Migration 0017 against real Postgres: the Tier-A memory firewall (CLAUDE.md
rule 3, ASSISTANT.md invariants #2/#4/#8).

Proves the load-bearing properties Postgres must enforce, not the tools:
- agent_memory is owner-only and domain-narrowed.
- an episodic trace is visible only to a session holding EVERY scope the turn
  touched — a multi-domain episode is never readable through a single scope (#4).
- episode pointers follow their episode's visibility, and a non-owner principal
  reads no agent memory at all (#8).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _add_memory(maker: async_sessionmaker, pid: str, code: str, tag: str) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.agent_memory"
                " (principal_id, domain_code, block_kind, body_md)"
                " VALUES (:pid, :code, 'core', :body)"
            ),
            {"pid": pid, "code": code, "body": f"{tag} {code}"},
        )


async def _add_episode(maker: async_sessionmaker, scopes: list[str], tag: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.agent_episodes (domain_scopes, body)"
                    " VALUES (:scopes, :body) RETURNING id"
                ),
                {"scopes": scopes, "body": tag},
            )
        ).scalar()
    return str(eid)


async def _episode_bodies(maker: async_sessionmaker, ctx: SessionContext, tag: str) -> list[str]:
    async with scoped_session(maker, ctx) as session:
        return list(
            (
                await session.execute(
                    text("SELECT body FROM app.agent_episodes WHERE body LIKE :t"), {"t": f"{tag}%"}
                )
            ).scalars()
        )


async def test_agent_memory_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health", "finance"):
        await _add_memory(maker, pid, code, tag)
    like = {"t": f"{tag}%"}

    # A health-only owner session reads only health memory — the firewall.
    health = read_context(pid, ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.agent_memory WHERE body_md LIKE :t"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # An unnarrowed owner reads all three.
    async with scoped_session(maker, OWNER) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_memory WHERE body_md LIKE :t"), like
            )
        ).scalar()
    assert count == 3

    # A non-owner principal reads none (invariant #8).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_memory WHERE body_md LIKE :t"), like
            )
        ).scalar()
    assert count == 0


async def test_narrowed_owner_cannot_write_memory_outside_scope(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    health = read_context(pid, ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.agent_memory"
                    " (principal_id, domain_code, block_kind, body_md)"
                    " VALUES (:pid, 'finance', 'core', 'sneaky')"
                ),
                {"pid": pid},
            )


async def test_multiscope_episode_visible_only_with_all_scopes(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = f"ep-{uuid.uuid4().hex[:8]}"
    # A turn that touched BOTH general and health.
    await _add_episode(maker, ["general", "health"], tag)

    # Neither single scope can read it — not decomposed into a general row (#4).
    assert await _episode_bodies(maker, read_context(pid, ("general",)), tag) == []
    assert await _episode_bodies(maker, read_context(pid, ("health",)), tag) == []

    # A session holding BOTH scopes reads it; so does the unnarrowed owner.
    assert await _episode_bodies(maker, read_context(pid, ("general", "health")), tag) == [tag]
    assert await _episode_bodies(maker, OWNER, tag) == [tag]


async def test_episode_refs_follow_their_episode(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = f"ref-{uuid.uuid4().hex[:8]}"
    eid = await _add_episode(maker, ["health"], tag)
    # A note to point at (owner-scoped insert).
    async with scoped_session(maker, OWNER) as session:
        note_id = (
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'health', 'n') RETURNING id"
                ),
                {"cid": f"{tag}-n"},
            )
        ).scalar()
        await session.execute(
            text("INSERT INTO app.agent_episode_refs (episode_id, note_id) VALUES (:eid, :nid)"),
            {"eid": eid, "nid": str(note_id)},
        )

    # Same query under each scope — only the session's RLS differs. A general-only
    # session cannot see the health episode's pointer (it follows the episode's
    # visibility); a health session can.
    count_refs = text("SELECT count(*) FROM app.agent_episode_refs WHERE episode_id = :eid")
    async with scoped_session(maker, read_context(pid, ("general",))) as session:
        assert (await session.execute(count_refs, {"eid": eid})).scalar() == 0
    async with scoped_session(maker, read_context(pid, ("health",))) as session:
        assert (await session.execute(count_refs, {"eid": eid})).scalar() == 1


async def test_non_owner_sees_no_episodes(maker: async_sessionmaker) -> None:
    await _owner_principal(maker)
    tag = f"no-{uuid.uuid4().hex[:8]}"
    await _add_episode(maker, ["health"], tag)
    # Even with the matching scope, a non-owner principal reads nothing (#8).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    assert await _episode_bodies(maker, token, tag) == []


async def test_note_deletion_purges_derived_episodes(maker: async_sessionmaker) -> None:
    """Purge is total (invariant #11): deleting a note deletes the episodic trace
    derived from it WHOLE — no agent-memory row keeps content from a deleted note."""
    await _owner_principal(maker)
    tag = f"purge-{uuid.uuid4().hex[:8]}"
    async with scoped_session(maker, OWNER) as session:
        note_id = (
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'health', 'lab') RETURNING id"
                ),
                {"cid": f"{tag}-n"},
            )
        ).scalar()
    eid = await _add_episode(maker, ["health"], tag)
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.agent_episode_refs (episode_id, note_id) VALUES (:eid, :nid)"),
            {"eid": eid, "nid": str(note_id)},
        )

    assert await SqlNotesRepo(maker).delete_note(OWNER, str(note_id)) is True

    async with scoped_session(maker, OWNER) as session:
        episodes = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_episodes WHERE id = :eid"), {"eid": eid}
            )
        ).scalar()
        refs = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_episode_refs WHERE note_id = :nid"),
                {"nid": str(note_id)},
            )
        ).scalar()
    assert episodes == 0  # the episode row is gone, not merely its pointer
    assert refs == 0  # the refs cascaded with the episode
