"""Migration 0173 against real Postgres: the jmolt scratchpad's M19 firewall matrix
(CLAUDE.md rule 3, JMOLT_PLAN §2 M19).

jmolt and jerv both run as the owner principal, so is_owner() can't separate them. The
policies do: SELECT is gated on the jmolt domain scope, WRITE on auth_context='jmolt'.
This proves (a) a jerv-scoped session reads but cannot write; (b) writes need
auth_context='jmolt'; (c) a session in neither domain sees nothing; (d) the archive is
append-only. Plus the app-level quota (16 files / 128 KB / 24 KB).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmoltscratchtools import build_jmolt_scratch_handlers
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import (
    MAX_FILE_BYTES,
    MAX_FILES,
    JmoltScratchRepo,
    QuotaError,
)
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# The shared testcontainer DB persists rows across tests; clear the LIVE scratch table
# (jmolt may DELETE it) before each test so file-count/list assertions are per-test. The
# append-only archive is left (it can't be deleted by design); tests filter it by unique
# per-test filenames.
_JMOLT_CLEAN = SessionContext(
    principal_kind="owner", domain_scopes=("jmolt",), auth_context="jmolt", owner_scoped=True
)


@pytest.fixture(autouse=True)
async def _clean_scratch(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _JMOLT_CLEAN) as s:
        await s.execute(text("DELETE FROM app.jmolt_scratch"))
    yield


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


def _jmolt_ctx(pid: str) -> SessionContext:
    # jmolt's nightly session: owner principal, jmolt domain scope, jmolt auth context.
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _jerv_observe_ctx(pid: str) -> SessionContext:
    # jerv's observation session: owner + jmolt read scope, but NO jmolt auth context.
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        owner_scoped=True,
    )


# A non-owner in neither domain.
_OUTSIDER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


async def test_jmolt_writes_and_reads_its_own_files(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltScratchRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.write(s, pid, "index.md", "who I want to remember: nobody yet")
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        assert await repo.read(s, pid, "index.md") == "who I want to remember: nobody yet"
        files = await repo.list_files(s, pid)
        assert [f.filename for f in files] == ["index.md"]
        # Every change is archived (append-only trail).
        hist = await repo.history(s, pid, "index.md")
        assert len(hist) == 1 and hist[0].op == "write"


async def test_jerv_can_read_but_never_write(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltScratchRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.write(s, pid, "notes.md", "the general submolt is loud")

    # jerv's observation session READS fine (jmolt domain scope grants SELECT).
    async with scoped_session(maker, _jerv_observe_ctx(pid)) as s:
        assert await repo.read(s, pid, "notes.md") == "the general submolt is loud"
        assert len(await repo.history(s, pid, "notes.md")) == 1

    # …but every WRITE is denied by RLS (no jmolt auth context).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _jerv_observe_ctx(pid)) as s:
            await repo.write(s, pid, "notes.md", "jerv tampering")

    # The file is unchanged.
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        assert await repo.read(s, pid, "notes.md") == "the general submolt is loud"


async def test_outsider_in_neither_domain_sees_nothing_and_cannot_write(
    maker: async_sessionmaker,
) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltScratchRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.write(s, pid, "secret.md", "jmolt's private notes")

    async with scoped_session(maker, _OUTSIDER) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.jmolt_scratch"))).scalar()
    assert count == 0  # RLS hides every row from a session without the jmolt scope.

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _OUTSIDER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.jmolt_scratch (principal_id, filename, content, bytes)"
                    " VALUES (:pid, 'x', 'y', 1)"
                ),
                {"pid": pid},
            )


async def test_archive_is_append_only_even_for_jmolt(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltScratchRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.write(s, pid, "a.md", "v1")
        await repo.write(s, pid, "a.md", "v2")  # a second archive row
    # jmolt cannot UPDATE or DELETE archive rows (no grant) — history is immutable.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _jmolt_ctx(pid)) as s:
            await s.execute(
                text("DELETE FROM app.jmolt_scratch_archive WHERE principal_id = :pid"),
                {"pid": pid},
            )
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        hist = await repo.history(s, pid, "a.md")
    assert [h.content for h in hist] == ["v2", "v1"]  # both versions, newest first


async def test_scratch_handlers_roundtrip_under_jmolt_context(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    handlers = build_jmolt_scratch_handlers(maker)
    ctx = ToolContext(session=_jmolt_ctx(pid), scopes=())

    assert "empty" in await handlers["scratch_list"]({}, ctx)
    assert "Saved" in await handlers["scratch_write"](
        {"filename": "index.md", "content": "hi"}, ctx
    )
    assert await handlers["scratch_read"]({"filename": "index.md"}, ctx) == "hi"
    assert "index.md" in await handlers["scratch_list"]({}, ctx)
    # An over-quota write returns the plain-language budget message, not an exception.
    over = await handlers["scratch_write"](
        {"filename": "big.md", "content": "x" * (MAX_FILE_BYTES + 1)}, ctx
    )
    assert "per-file limit" in over
    assert "Deleted" in await handlers["scratch_write"](
        {"filename": "index.md", "mode": "delete"}, ctx
    )


async def test_quota_rejects_oversize_file_and_too_many_files(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltScratchRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        with pytest.raises(QuotaError):
            await repo.write(s, pid, "big.md", "x" * (MAX_FILE_BYTES + 1))
    # Fill to the file-count limit, then the next new file is refused.
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        for i in range(MAX_FILES):
            await repo.write(s, pid, f"f{i}.md", "small")
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        with pytest.raises(QuotaError):
            await repo.write(s, pid, "one-too-many.md", "small")
        # Overwriting an EXISTING file at the limit is still fine.
        await repo.write(s, pid, "f0.md", "updated")
