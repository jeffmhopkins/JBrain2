"""jmolt_observe against real Postgres (docs/plans/JMOLT_PLAN.md, W4, M16).

jerv's read-only lens on jmolt. This proves the umbrella (a) reads jmolt's nights,
transcript, actions, scratchpad, and outbox and fences every return; (b) reads jmolt's
OWN tables under a NON-owner jmolt-scoped context, so the M19(a) split makes the
read-only guarantee MECHANICAL — SELECT is granted by the scope, but every write is
denied by RLS (is_owner() false, auth_ctx() not 'jmolt'); and (c) refuses to run in a
turn that also holds an egress tool (M16), so a poisoned diary can never meet a live
web/email/Moltbook call in the same turn.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmoltobservetools import (
    _jmolt_data_read_ctx,
    build_jmolt_observe_handlers,
)
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The observer persona's whole tool surface — the M16 guard permits only these.
_OBSERVER_TOOLS = frozenset({"jmolt_observe", "current_time"})


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _jmolt_ctx(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
    async with scoped_session(maker, _jmolt_ctx("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_scratch"))
    yield


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


def _observe_ctx() -> ToolContext:
    # The observer's turn: its session context is irrelevant (the handler opens its own
    # jmolt-read context), but `agent_tools` is the M16 gate — the safe set passes.
    return ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), agent_tools=_OBSERVER_TOOLS
    )


async def _seed(maker: async_sessionmaker, pid: str) -> None:
    """A little of everything jmolt leaves behind: a scratch file, a staged write, and a
    logged action — all written under jmolt's own context."""
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await JmoltScratchRepo().write(s, pid, "index.md", "who I want to remember: nobody yet")
        await OutboxRepo().stage(s, pid, kind="comment", payload={"post_id": "p1", "content": "hi"})
        await ActionLedgerRepo().record(s, pid, action="publish_comment", target="p1")


async def test_observe_reads_and_fences_every_kind(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _seed(maker, pid)
    observe = build_jmolt_observe_handlers(maker)["jmolt_observe"]
    ctx = _observe_ctx()

    files = await observe({"action": "scratch_list"}, ctx)
    assert "index.md" in files and "material to observe" in files  # fenced

    body = await observe({"action": "scratch_read", "filename": "index.md"}, ctx)
    assert "nobody yet" in body

    hist = await observe({"action": "scratch_history"}, ctx)
    assert "index.md" in hist

    actions = await observe({"action": "actions"}, ctx)
    assert "publish_comment" in actions and "p1" in actions

    outbox = await observe({"action": "outbox"}, ctx)
    assert "comment" in outbox and "queued" in outbox


async def test_observe_reads_a_night_transcript(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    # Seed a jmolt session + one turn (its transcript), written under jmolt's context.
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        sid = (
            await s.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                    " VALUES (gen_random_uuid(), :pid, 'jmolt', ARRAY['jmolt']) RETURNING id"
                ),
                {"pid": pid},
            )
        ).scalar()
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content)"
                " VALUES (gen_random_uuid(), :sid, 'assistant',"
                " 'read the general submolt; it is mostly noise')"
            ),
            {"sid": sid},
        )
    out = await build_jmolt_observe_handlers(maker)["jmolt_observe"](
        {"action": "transcript"}, _observe_ctx()
    )
    assert "mostly noise" in out and "material to observe" in out


async def test_observe_refuses_alongside_an_egress_tool(maker: async_sessionmaker) -> None:
    # M16 — the umbrella must not run in a turn that can also act on what it reads.
    pid = await _owner_pid(maker)
    await _seed(maker, pid)
    observe = build_jmolt_observe_handlers(maker)["jmolt_observe"]
    poisoned = ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        agent_tools=frozenset({"jmolt_observe", "current_time", "web_fetch"}),
    )
    out = await observe({"action": "scratch_read", "filename": "index.md"}, poisoned)
    assert "refuses to run" in out and "web_fetch" in out
    assert "nobody yet" not in out  # nothing from jmolt's record leaked into the egress turn


async def test_observe_unknown_action_is_a_plain_message(maker: async_sessionmaker) -> None:
    observe = build_jmolt_observe_handlers(maker)["jmolt_observe"]
    out = await observe({"action": "delete_everything"}, _observe_ctx())
    assert "needs action=" in out


async def test_observe_data_context_can_read_but_rls_denies_every_write(
    maker: async_sessionmaker,
) -> None:
    # M19(a): the context the observe umbrella uses for jmolt's OWN tables grants SELECT
    # (the scope) but is denied every INSERT/UPDATE/DELETE by RLS — is_owner() is false and
    # auth_ctx() is not 'jmolt', so the read-only guarantee is mechanical, not a convention.
    pid = await _owner_pid(maker)
    await _seed(maker, pid)
    read = _jmolt_data_read_ctx(pid)

    # SELECT works (the scope grants it).
    async with scoped_session(maker, read) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.jmolt_scratch"))).scalar()
        assert (count or 0) >= 1

    # An INSERT is refused outright (the WITH CHECK on the scratch write policy fails).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, read) as s:
            await s.execute(
                text(
                    "INSERT INTO app.jmolt_scratch (principal_id, filename, content, bytes)"
                    " VALUES (:pid, 'x', 'y', 1)"
                ),
                {"pid": pid},
            )
    # An UPDATE/DELETE modifies ZERO rows — the outbox-advance / ledger-prune policies
    # (is_owner() AND auth_ctx()<>'jmolt') exclude every row from this non-owner context,
    # so the observer can neither release nor purge anything.
    async with scoped_session(maker, read) as s:
        upd = await s.execute(text("UPDATE app.jmolt_outbox SET status = 'published'"))
        assert upd.rowcount == 0  # type: ignore[attr-defined]
        dele = await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
        assert dele.rowcount == 0  # type: ignore[attr-defined]
    # The rows are untouched (a jmolt-context read still sees the seeded outbox row).
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        assert len(await OutboxRepo().list_by_status(s, pid, ("queued",))) == 1
