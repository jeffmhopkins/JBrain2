"""Migration 0015 against real Postgres: the owner-narrowable domain firewall and
agent_sessions RLS (CLAUDE.md rule 3, ASSISTANT.md invariant #4).

Proves the load-bearing security property: a narrowed owner session is restricted
to its selected domains by Postgres, not by the tools — while an ordinary owner
session still sees everything (the backward-compatibility regression).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.agents import NON_OWNER_PERSONAS, OWNER_AGENTS
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
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


async def _seed_notes(maker: async_sessionmaker, run: str) -> None:
    async with scoped_session(maker, OWNER) as session:
        for code in ("general", "health", "finance"):
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, :code, :body)"
                ),
                {"cid": f"{run}-{code}", "code": code, "body": f"{run} {code}"},
            )


async def test_owner_scoped_narrows_domain_reads(maker: async_sessionmaker) -> None:
    run = uuid.uuid4().hex[:8]
    await _seed_notes(maker, run)
    like = {"p": f"{run}-%"}

    # A narrowed (health-only) owner sees ONLY health — the firewall, not a filter.
    health = read_context(str(uuid.uuid4()), ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.notes WHERE client_id LIKE :p"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # Regression: an ordinary (unnarrowed) owner still sees all three domains.
    async with scoped_session(maker, OWNER) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM app.notes WHERE client_id LIKE :p"), like
            )
        ).scalar()
    assert count == 3


async def test_narrowed_owner_cannot_write_outside_scope(maker: async_sessionmaker) -> None:
    health = read_context(str(uuid.uuid4()), ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'finance', 'sneaky')"
                ),
                {"cid": f"sneak-{uuid.uuid4().hex[:8]}"},
            )


async def test_agent_sessions_are_owner_only(maker: async_sessionmaker) -> None:
    auth = SqlAuthRepo(maker)
    await service.rotate_owner_key(auth)
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")

    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["health"], title="health cleanup")
    assert info.domain_scopes == ("health",)
    assert len(await repo.list(owner)) == 1

    # A non-owner principal sees no sessions at all.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    assert await repo.list(token) == []

    # A *narrowed* owner still sees its sessions — owner_scoped restricts domain
    # data, never owner-only tables (it keeps owner identity).
    narrowed = read_context(str(pid), ("general",))
    assert len(await repo.list(narrowed)) == 1


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_agent_persona_round_trips_and_defaults_to_curator(
    maker: async_sessionmaker,
) -> None:
    """Migration 0070: the selected agent persists and reads back; an unspecified
    agent defaults to the Full Brain curator (backward-compatible)."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)

    default = await repo.create(owner, domain_scopes=["general"], title="default")
    assert default.agent == "curator"
    assert (await repo.get(owner, default.id)).agent == "curator"  # type: ignore[union-attr]

    chatbot = await repo.create(owner, domain_scopes=[], title="ask jerv", agent="jerv")
    assert chatbot.agent == "jerv"
    assert (await repo.get(owner, chatbot.id)).agent == "jerv"  # type: ignore[union-attr]


async def test_every_owner_persona_satisfies_the_check(maker: async_sessionmaker) -> None:
    """The DB CHECK (0070, widened in 0095) must admit every OWNER-selectable persona —
    a name in OWNER_AGENTS but not the constraint fails session create with a CHECK
    violation (the archivist regression). Guards the two from drifting apart.

    The NON-owner intake persona is deliberately NOT in this set: it must never reach
    app.agent_sessions (§5), proven by the rejection below."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    for name in sorted(OWNER_AGENTS):
        info = await repo.create(owner, domain_scopes=[], title=name, agent=name)
        assert (await repo.get(owner, info.id)).agent == name  # type: ignore[union-attr]


async def test_non_owner_persona_is_rejected_by_the_agent_check(maker: async_sessionmaker) -> None:
    """The DB-level proof of §5: the intake persona can NEVER be stored owner-side — the
    agent_sessions CHECK excludes it, so even a bypass of the API's is_owner_agent gate
    fails closed at the database."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    for name in sorted(NON_OWNER_PERSONAS):
        with pytest.raises((ProgrammingError, IntegrityError)):
            await repo.create(owner, domain_scopes=[], title=name, agent=name)


async def test_agent_check_constraint_rejects_unknown_persona(maker: async_sessionmaker) -> None:
    """The DB CHECK pins `agent` to the closed set, so a malformed value can never
    reach the turn loop — defense in depth behind the API's validation."""
    owner = await _owner_ctx(maker)
    with pytest.raises((ProgrammingError, IntegrityError)):
        async with scoped_session(maker, owner) as session:
            await session.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes, agent)"
                    " VALUES (gen_random_uuid(), :pid, '{}', 'rogue')"
                ),
                {"pid": owner.principal_id},
            )


async def test_rename_updates_the_title(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="old")
    await repo.rename(owner, info.id, "new name")
    assert (await repo.get(owner, info.id)).title == "new name"  # type: ignore[union-attr]


async def test_record_context_round_trips(maker: async_sessionmaker) -> None:
    # A fresh session carries no fill; recording a turn's context persists it so get/list
    # restore the meter on reopen.
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="chat")
    fresh = await repo.get(owner, info.id)
    assert fresh is not None
    assert fresh.context_tokens is None and fresh.context_window is None

    await repo.record_context(owner, info.id, 9000, 131072)
    got = await repo.get(owner, info.id)
    assert got is not None
    assert got.context_tokens == 9000 and got.context_window == 131072
    # It also rides the list view (the Chats cards), so a reopen reads it without a turn.
    listed = next(s for s in await repo.list(owner) if s.id == info.id)
    assert listed.context_tokens == 9000 and listed.context_window == 131072


async def test_set_scopes_rescopes_and_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="scratch")

    # A non-owner cannot re-scope it (RLS hides the row); scope is unchanged.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    await repo.set_scopes(token, info.id, ["general", "health"])
    assert (await repo.get(owner, info.id)).domain_scopes == ("general",)  # type: ignore[union-attr]

    # The owner widens then narrows it.
    await repo.set_scopes(owner, info.id, ["general", "health"])
    assert (await repo.get(owner, info.id)).domain_scopes == ("general", "health")  # type: ignore[union-attr]
    await repo.set_scopes(owner, info.id, ["health"])
    assert (await repo.get(owner, info.id)).domain_scopes == ("health",)  # type: ignore[union-attr]


async def test_list_aggregates_turns_preview_and_staged(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="recap")
    run_id = await AgentRunLog(maker).start(owner, session_id=info.id, prompt_version="v")
    await AgentTranscript(maker).record_exchange(
        owner,
        session_id=info.id,
        run_id=run_id,
        user_text="what's open?",
        assistant_text="two labs",
        tools=[],
    )
    # A staged Proposal linked to this session.
    async with scoped_session(maker, owner) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.proposals"
                " (id, session_id, principal_id, kind, status, domain_code)"
                " VALUES (gen_random_uuid(), :sid, :pid, 'correction', 'staged', 'general')"
            ),
            {"sid": info.id, "pid": pid},
        )

    card = next(c for c in await repo.list(owner) if c.id == info.id)
    assert card.turn_count == 1  # one user turn
    assert card.preview == "two labs"  # the latest turn, the resume hint
    assert card.staged_count == 1


async def test_set_status_archives_and_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="scratch")
    assert info.status == "active"

    # A non-owner cannot flip it (RLS hides the row); it stays active.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    await repo.set_status(token, info.id, "archived")
    assert (await repo.get(owner, info.id)).status == "active"  # type: ignore[union-attr]

    # The owner archives it, then restores it.
    await repo.set_status(owner, info.id, "archived")
    assert (await repo.get(owner, info.id)).status == "archived"  # type: ignore[union-attr]
    await repo.set_status(owner, info.id, "active")
    assert (await repo.get(owner, info.id)).status == "active"  # type: ignore[union-attr]


async def test_subagent_lineage_round_trips_and_is_owner_only(maker: async_sessionmaker) -> None:
    """Migration 0105: a spawned child carries parent_session_id/depth/no_memory,
    which read back; a root defaults cleanly (parent=None, depth=0, no_memory=False).
    The lineage columns are owner-only metadata (m3) — a non-owner sees no rows."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)

    root = await repo.create(owner, domain_scopes=[], title="ask jerv", agent="jerv")
    assert root.parent_session_id is None
    assert root.depth == 0
    assert root.no_memory is False

    child = await repo.create(
        owner,
        domain_scopes=[],
        title="researcher",
        agent="research",
        parent_session_id=root.id,
        depth=1,
        no_memory=True,
    )
    read = await repo.get(owner, child.id)
    assert read is not None
    assert read.parent_session_id == root.id
    assert read.depth == 1
    assert read.no_memory is True

    # Owner-only: a non-owner sees neither the parent nor the child.
    token = SessionContext(principal_kind="capability_token", domain_scopes=())
    assert await repo.list(token) == []


async def test_parent_delete_cascades_subagent_children(maker: async_sessionmaker) -> None:
    """parent_session_id is ON DELETE CASCADE — deleting a parent takes its children
    with it (children are sub-state of the parent turn, never orphaned top-level)."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    root = await repo.create(owner, domain_scopes=[], title="parent", agent="jerv")
    child = await repo.create(
        owner, domain_scopes=[], title="child", agent="research", parent_session_id=root.id, depth=1
    )
    await repo.delete(owner, root.id)
    assert await repo.get(owner, child.id) is None


async def test_list_reports_subagent_count_and_parent_link(maker: async_sessionmaker) -> None:
    """The Wave-S4 nested-rail metadata: a parent's list row carries how many direct
    children it spawned, and each child carries its parent_session_id (so the PWA can
    nest it and drop it from top-level bucketing)."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    root = await repo.create(owner, domain_scopes=[], title="parent", agent="jerv")
    for label in ("a", "b"):
        await repo.create(
            owner,
            domain_scopes=[],
            title=label,
            agent="research",
            parent_session_id=root.id,
            depth=1,
        )
    by_id = {s.id: s for s in await repo.list(owner)}
    assert by_id[root.id].subagent_count == 2
    assert by_id[root.id].parent_session_id is None
    children = [s for s in by_id.values() if s.parent_session_id == root.id]
    assert len(children) == 2
    assert all(c.subagent_count == 0 for c in children)


async def test_depth_check_rejects_out_of_range(maker: async_sessionmaker) -> None:
    """The depth CHECK (0..2) makes the two-sub-agent-layer cap structural at the
    table — a depth past the leaf can never be written, with no model cooperation."""
    owner = await _owner_ctx(maker)
    with pytest.raises((ProgrammingError, IntegrityError)):
        async with scoped_session(maker, owner) as session:
            await session.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes, agent, depth)"
                    " VALUES (gen_random_uuid(), :pid, '{}', 'research', 3)"
                ),
                {"pid": owner.principal_id},
            )


async def test_runs_kind_admits_subagent_with_parent(maker: async_sessionmaker) -> None:
    """Migration 0105 widens the runs.kind CHECK to admit 'subagent'; a child run
    persists with parent_run_id set for the tree cost rollup."""
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    runlog = AgentRunLog(maker)
    root = await repo.create(owner, domain_scopes=[], title="parent", agent="jerv")
    root_run = await runlog.start(owner, session_id=root.id, prompt_version="jerv-v1")
    child = await repo.create(
        owner, domain_scopes=[], title="child", agent="research", parent_session_id=root.id, depth=1
    )
    child_run = await runlog.start(
        owner,
        session_id=child.id,
        prompt_version="research-v1",
        kind="subagent",
        parent_run_id=root_run,
    )
    async with scoped_session(maker, owner) as session:
        row = (
            await session.execute(
                text("SELECT kind, parent_run_id FROM app.runs WHERE id = :id"), {"id": child_run}
            )
        ).one()
    assert row.kind == "subagent"
    assert str(row.parent_run_id) == root_run


async def test_runs_kind_rejects_unknown_value(maker: async_sessionmaker) -> None:
    """The widened CHECK is still closed — a bogus kind is refused at the DB."""
    owner = await _owner_ctx(maker)
    with pytest.raises((ProgrammingError, IntegrityError)):
        async with scoped_session(maker, owner) as session:
            await session.execute(
                text(
                    "INSERT INTO app.runs (id, kind, ran_as) "
                    "VALUES (gen_random_uuid(), 'rogue', 'scoped')"
                )
            )


async def test_delete_cascades_runs_and_transcript_and_is_owner_only(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="scratch")
    run_id = await AgentRunLog(maker).start(owner, session_id=info.id, prompt_version="v")
    await AgentTranscript(maker).record_exchange(
        owner, session_id=info.id, run_id=run_id, user_text="q", assistant_text="a", tools=[]
    )

    # A non-owner cannot delete it (RLS blocks the row); it survives.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    await repo.delete(token, info.id)
    assert await repo.get(owner, info.id) is not None

    # The owner deletes it; the run and the transcript cascade away.
    await repo.delete(owner, info.id)
    assert await repo.get(owner, info.id) is None
    async with scoped_session(maker, owner) as session:
        runs = (
            await session.execute(
                text("SELECT count(*) FROM app.runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar()
        turns = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_turns WHERE session_id = :id"), {"id": info.id}
            )
        ).scalar()
    assert runs == 0 and turns == 0
