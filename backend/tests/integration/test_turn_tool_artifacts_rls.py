"""Cross-turn tool artifacts against real Postgres: RLS isolation per CLAUDE.md rule 3.

A tool artifact carries a `domain_code` firewall (the same has_domain_scope policy as a
chat attachment): a health-scoped artifact is visible only to a health-scoped read, a
scoped principal cannot stamp an out-of-scope domain_code, and the empty-scoped jerv
session round-trips its own 'general'-stamped artifacts.
"""

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.tool_artifacts import ToolArtifactRepo, artifact_domain, artifact_scopes
from jbrain.db.session import SessionContext
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401
from tests.integration.test_turn_attachments_rls import (  # noqa: F401
    _owner_principal,
    _session,
    maker,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
def sessions(maker: async_sessionmaker) -> AgentSessionRepo:  # noqa: F811
    return AgentSessionRepo(maker)


@pytest.fixture
def repo(maker: async_sessionmaker, sessions: AgentSessionRepo) -> ToolArtifactRepo:  # noqa: F811
    return ToolArtifactRepo(maker, sessions)


async def _remember(
    repo: ToolArtifactRepo,
    ctx: SessionContext,
    session_id: str,
    *,
    url: str,
    domain: str,
    kind: str = "web_fetch",
    total: int = 1234,
):
    return await repo.remember(
        ctx,
        session_id,
        kind=kind,
        source_url=url,
        title="t",
        sha256="aa" * 32,
        total_chars=total,
        domain_code=domain,
    )


async def test_artifact_domain_firewall(
    repo: ToolArtifactRepo,
    sessions: AgentSessionRepo,
    maker: async_sessionmaker,  # noqa: F811
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")  # full owner, all scopes
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    art = await _remember(repo, health, session_id, url="https://x/1", domain="health")

    # Visible inside the health scope; invisible to general-only and to an unscoped read.
    assert await repo.get(health, art.id) is not None
    assert await repo.get(general, art.id) is None
    assert await repo.get(UNSCOPED, art.id) is None
    # The owner (full scope) still sees it.
    assert await repo.get(owner, art.id) is not None


async def test_scoped_principal_cannot_stamp_out_of_scope_domain(
    repo: ToolArtifactRepo,
    sessions: AgentSessionRepo,
    maker: async_sessionmaker,  # noqa: F811
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("general",))
    # A general-only session physically cannot stamp a health-scoped artifact: the WITH
    # CHECK clause rejects the insert.
    with pytest.raises(ProgrammingError):
        await _remember(repo, general, session_id, url="https://x/2", domain="health")


async def test_set_offset_cannot_advance_across_the_firewall(
    repo: ToolArtifactRepo,
    sessions: AgentSessionRepo,
    maker: async_sessionmaker,  # noqa: F811
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    art = await _remember(repo, health, session_id, url="https://x/3", domain="health")

    # A general-scoped session can't see the health artifact, so its cursor update matches
    # nothing — the RLS predicate hides the row, not just the read.
    await repo.set_offset(general, art.id, 999)
    assert (await repo.get(health, art.id)).last_offset == 0
    # The in-scope update lands.
    await repo.set_offset(health, art.id, 500)
    assert (await repo.get(health, art.id)).last_offset == 500


async def test_empty_scope_session_round_trips_its_general_artifact(
    repo: ToolArtifactRepo,
    sessions: AgentSessionRepo,
    maker: async_sessionmaker,  # noqa: F811
) -> None:
    """jerv is empty-scoped: its artifact is stamped 'general', and it reads/writes it
    under artifact_scopes(()) = ('general',) — the same asymmetry chat attachments use."""
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    session_id = await _session(sessions, owner, ())  # empty scopes, like jerv

    resolved = await repo.session_context(owner, session_id)
    assert resolved is not None
    jerv_ctx, domain = resolved
    assert domain == artifact_domain(())  # 'general'
    assert set(artifact_scopes(())) == {"general"}

    art = await _remember(repo, jerv_ctx, session_id, url="https://x/4", domain=domain)
    assert (await repo.get(jerv_ctx, art.id)) is not None
    listed = await repo.list_for_session(jerv_ctx, session_id, limit=5)
    assert [a.id for a in listed] == [art.id]


async def test_remember_upserts_the_same_url(
    repo: ToolArtifactRepo,
    sessions: AgentSessionRepo,
    maker: async_sessionmaker,  # noqa: F811
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("general",))

    first = await _remember(
        repo, general, session_id, url="https://x/5", domain="general", total=10
    )
    await repo.set_offset(general, first.id, 5)
    # Re-fetching the same URL refreshes the one row (new length) and resets the cursor.
    second = await _remember(
        repo, general, session_id, url="https://x/5", domain="general", total=99
    )
    assert second.id == first.id
    assert second.total_chars == 99
    assert (await repo.get(general, first.id)).last_offset == 0
    assert len(await repo.list_for_session(general, session_id, limit=10)) == 1
