"""Chat-turn attachments against real Postgres: RLS isolation per CLAUDE.md rule 3.

A turn attachment carries a `domain_code` firewall (the same has_domain_scope policy
as note attachments): a health-scoped file is visible only to a health-scoped read,
and a scoped principal cannot insert an out-of-scope domain_code.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.attachments import (
    TurnAttachmentRepo,
    attachment_scopes,
    domain_for_session,
)
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def sessions(maker: async_sessionmaker) -> AgentSessionRepo:
    return AgentSessionRepo(maker)


@pytest.fixture
def repo(maker: async_sessionmaker, sessions: AgentSessionRepo) -> TurnAttachmentRepo:
    return TurnAttachmentRepo(maker, sessions)


async def _owner_principal(maker: async_sessionmaker) -> str:
    """A real owner principal id — agent_sessions FK requires one, so the synthetic
    OWNER.principal_id from test_rls won't do."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _session(
    sessions: AgentSessionRepo, owner: SessionContext, scopes: tuple[str, ...]
) -> str:
    info = await sessions.create(owner, domain_scopes=list(scopes))
    return info.id


async def test_turn_attachment_domain_firewall(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")  # full owner, all scopes
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="aa" * 32,
        filename="scan.png",
        media_type="image/png",
        size_bytes=3,
        domain_code="health",
    )

    # Visible inside the health scope; invisible to general-only and to an unscoped read.
    assert await repo.get(health, att.id) is not None
    assert await repo.get(general, att.id) is None
    assert await repo.get(UNSCOPED, att.id) is None
    # The owner (full scope) still sees it.
    assert await repo.get(owner, att.id) is not None


async def test_scoped_principal_cannot_insert_out_of_scope_domain(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("general",))
    # A general-only session physically cannot stamp a health-scoped attachment:
    # the WITH CHECK clause rejects the insert.
    with pytest.raises(ProgrammingError):
        await repo.add(
            general,
            session_id,
            sha256="bb" * 32,
            filename="x.pdf",
            media_type="application/pdf",
            size_bytes=1,
            domain_code="health",
        )


async def test_remove_respects_firewall(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="cc" * 32,
        filename="scan.png",
        media_type="image/png",
        size_bytes=2,
        domain_code="health",
    )
    # Out-of-scope delete reads as missing; in-scope delete returns the id.
    assert await repo.remove(general, att.id) is None
    assert await repo.remove(health, att.id) == att.id
    assert await repo.get(owner, att.id) is None


async def test_list_for_session_only_returns_in_scope_rows(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    await repo.add(
        health,
        session_id,
        sha256="dd" * 32,
        filename="a.png",
        media_type="image/png",
        size_bytes=1,
        domain_code="health",
    )
    assert len(await repo.list_for_session(health, session_id)) == 1
    # A general-only read of the same session sees no health-scoped file.
    assert await repo.list_for_session(general, session_id) == []


async def test_analysis_cache_and_thumb_respect_the_firewall(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    """The cached analyze_video result + its frame thumbnails are firewalled: a health
    clip's analysis (and a thumb-by-id check) is invisible to a general/unscoped read,
    and a sha that isn't one of the attachment's frames is never returned."""
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="a1" * 32,
        filename="clip.mp4",
        media_type="video/mp4",
        size_bytes=9,
        domain_code="health",
    )
    analysis = {
        "summary": "a clinic walkthrough",
        "frames": [{"t_ms": 0, "caption": "a sign", "thumb_id": "thumb-sha-1"}],
        "transcript": None,
    }
    await repo.set_analysis(health, att.id, analysis)

    # Readable in scope; invisible to general-only and to an unscoped read.
    got = await repo.analysis(health, att.id)
    assert got is not None and got["summary"] == "a clinic walkthrough"
    assert await repo.analysis(general, att.id) is None
    assert await repo.analysis(UNSCOPED, att.id) is None

    # The thumbnail is served only for a member sha, only in scope.
    assert await repo.frame_thumb(health, att.id, "thumb-sha-1") == "thumb-sha-1"
    assert await repo.frame_thumb(health, att.id, "not-a-frame") is None  # never a foreign blob
    assert await repo.frame_thumb(general, att.id, "thumb-sha-1") is None  # firewall


async def test_set_analysis_cannot_write_across_the_firewall(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # A general-only session can't see the health row, so set_analysis matches nothing —
    # it can neither plant nor overwrite an out-of-scope attachment's analysis.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="b2" * 32,
        filename="clip.mp4",
        media_type="video/mp4",
        size_bytes=4,
        domain_code="health",
    )
    await repo.set_analysis(general, att.id, {"summary": "sneak", "frames": []})
    assert await repo.analysis(health, att.id) is None  # the out-of-scope write was a no-op


async def test_bind_to_turn_and_transcript_replay(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # A user turn's attachments bind to its AgentTurn row and replay on load.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="ee" * 32,
        filename="scan.png",
        media_type="image/png",
        size_bytes=4,
        domain_code="health",
    )
    transcript = AgentTranscript(maker, repo)
    user_turn_id = await transcript.record_exchange(
        owner,
        session_id=session_id,
        run_id=None,
        user_text="what is this?",
        assistant_text="a scan",
        tools=[],
    )
    await repo.bind_to_turn(health, [att.id], user_turn_id)

    # list_for_turns returns the bound row keyed by its user turn id.
    by_turn = await repo.list_for_turns(health, [user_turn_id])
    assert [a.id for a in by_turn[user_turn_id]] == [att.id]

    # The transcript replays the file on the USER turn; the assistant turn carries none.
    turns = await transcript.load(owner, session_id)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert [a.filename for a in turns[0].attachments] == ["scan.png"]
    assert turns[1].attachments == []


async def test_turn_cannot_reference_an_out_of_scope_attachment(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # An attachment uploaded under one firewall cannot be bound to a turn through a
    # session narrowed to a DIFFERENT scope: RLS makes the row invisible to the bind,
    # so the UPDATE matches nothing and the turn never references the foreign file.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="ff" * 32,
        filename="labs.png",
        media_type="image/png",
        size_bytes=4,
        domain_code="health",
    )
    transcript = AgentTranscript(maker, repo)
    user_turn_id = await transcript.record_exchange(
        owner,
        session_id=session_id,
        run_id=None,
        user_text="smuggle?",
        assistant_text="no",
        tools=[],
    )
    # A general-scoped bind can't see the health-scoped row, so nothing is bound.
    await repo.bind_to_turn(general, [att.id], user_turn_id)
    assert await repo.list_for_turns(health, [user_turn_id]) == {}
    # The health-scoped bind (the legitimate one) does attach it.
    await repo.bind_to_turn(health, [att.id], user_turn_id)
    assert [a.id for a in (await repo.list_for_turns(health, [user_turn_id]))[user_turn_id]] == [
        att.id
    ]


async def test_empty_scope_session_round_trips_its_attachment(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # A Jerv/Teacher session (empty scopes) stamps 'general' and must be able to WRITE,
    # READ, BIND and REPLAY its own attachment under the widened attachment context —
    # the bug N1 fixed: the bare empty-scope context could neither pass the INSERT's
    # WITH CHECK has_domain_scope('general') nor read the row back.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    session_id = await _session(sessions, owner, ())  # empty scopes (Jerv/Teacher)

    domain_code = domain_for_session(())  # 'general'
    # The context the upload endpoint / chat handler now use: scopes + stamped domain.
    att_ctx = read_context(pid, attachment_scopes(()))
    assert "general" in att_ctx.domain_scopes
    # Upload succeeds (no WITH CHECK failure) — the heart of the fix.
    att = await repo.add(
        att_ctx,
        session_id,
        sha256="11" * 32,
        filename="jerv.png",
        media_type="image/png",
        size_bytes=3,
        domain_code=domain_code,
    )
    assert att.domain_code == "general"
    # The same context reads its own file back (previously a silent miss).
    assert await repo.get(att_ctx, att.id) is not None

    # Reference it on a chat turn: bind + replay return it.
    transcript = AgentTranscript(maker, repo)
    user_turn_id = await transcript.record_exchange(
        owner,
        session_id=session_id,
        run_id=None,
        user_text="look at this",
        assistant_text="ok",
        tools=[],
    )
    await repo.bind_to_turn(att_ctx, [att.id], user_turn_id)
    by_turn = await repo.list_for_turns(att_ctx, [user_turn_id])
    assert [a.filename for a in by_turn[user_turn_id]] == ["jerv.png"]

    # Isolation preserved: a finance-only session still cannot read this 'general' file.
    finance = read_context(pid, ("finance",))
    assert await repo.get(finance, att.id) is None


async def test_multi_scope_session_round_trips_its_general_attachment(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # A multi-scope session also stamps 'general'; the widened context (its scopes PLUS
    # 'general') must write/read it, while a single foreign domain still cannot.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    session_id = await _session(sessions, owner, ("health", "finance"))

    att_ctx = read_context(pid, attachment_scopes(("health", "finance")))
    assert set(att_ctx.domain_scopes) == {"health", "finance", "general"}
    att = await repo.add(
        att_ctx,
        session_id,
        sha256="22" * 32,
        filename="multi.pdf",
        media_type="application/pdf",
        size_bytes=2,
        domain_code=domain_for_session(("health", "finance")),
    )
    assert att.domain_code == "general"
    assert await repo.get(att_ctx, att.id) is not None
    # A location-only session (none of these scopes) cannot read the 'general' file.
    location = read_context(pid, ("location",))
    assert await repo.get(location, att.id) is None


def test_attachment_scopes_rule() -> None:
    # Single scope → just that scope (curator isolation unchanged); empty/multi → the
    # scopes PLUS the stamped 'general'.
    assert attachment_scopes(("health",)) == ("health",)
    assert attachment_scopes(()) == ("general",)
    assert set(attachment_scopes(("health", "finance"))) == {"health", "finance", "general"}


def test_domain_for_session_rule() -> None:
    # Single domain → that domain; zero or multiple → the shared 'general' scope.
    assert domain_for_session(("health",)) == "health"
    assert domain_for_session(()) == "general"
    assert domain_for_session(("health", "finance")) == "general"
