"""jmolt's Moltbook write tools stage into the outbox with the M8/M9/M10 guards, against
real Postgres (the outbox RLS + the tool logic together)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.loop import ToolContext
from jbrain.agent.moltbookwritetools import build_moltbook_write_handlers
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(
        maker, SessionContext(principal_kind="owner", domain_scopes=("jmolt",))
    ) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
    yield


def _ctx(pid: str) -> ToolContext:
    return ToolContext(session=_jmolt(pid), scopes=(), timezone="UTC")


async def test_post_stages_into_outbox_with_a_publish_time(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    out = await h["moltbook_post"](
        {"submolt": "general", "title": "the quiet submolts are the good ones", "content": "..."},
        _ctx(pid),
    )
    assert "Staged" in out
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    assert len(rows) == 1 and rows[0].kind == "post" and rows[0].publish_at is not None


async def test_post_is_blocked_by_content_lint(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    out = await h["moltbook_post"](
        {"submolt": "general", "title": "buy $MOLT now before the presale"}, _ctx(pid)
    )
    assert "blocked" in out
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await OutboxRepo().list_by_status(s, pid, ("queued",)) == []  # nothing staged


async def test_post_rejects_near_duplicate(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    title = "Three weeks in and the general submolt is still mostly noise and duplicate posts"
    assert "Staged" in await h["moltbook_post"]({"submolt": "general", "title": title}, _ctx(pid))
    out = await h["moltbook_post"]({"submolt": "general", "title": title + " tonight"}, _ctx(pid))
    assert "too similar" in out


async def test_profile_update_prepends_the_fixed_disclosure(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()
    store.values["moltbook_disclosure"] = "Autonomous experiment; a human reads the logs."
    h = build_moltbook_write_handlers(maker, store)  # type: ignore[arg-type]
    await h["moltbook_profile_update"]({"bio": "I like tide pools."}, _ctx(pid))
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    desc = rows[0].payload["description"]
    assert desc.startswith("Autonomous experiment") and "tide pools" in desc


async def test_comment_stages_and_is_recorded(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    await h["moltbook_comment"](
        {"post_id": "p1", "content": "answering the thing you asked"}, _ctx(pid)
    )
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
        ledger = await ActionLedgerRepo().recent(s, pid)
    assert rows[0].kind == "comment"
    assert any(r.action == "stage_comment" for r in ledger)
