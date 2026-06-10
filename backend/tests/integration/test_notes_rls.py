"""Notes/attachments against real Postgres: RLS isolation per CLAUDE.md rule 3,
plus the idempotency and FK behaviors the unit fakes can only imitate."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import UnknownDomain
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def repo(database_url: str) -> AsyncIterator[SqlNotesRepo]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield SqlNotesRepo(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


async def test_create_is_idempotent_against_real_unique_constraint(repo: SqlNotesRepo) -> None:
    first, created1 = await repo.create_note(
        OWNER, client_id="idem-1", domain="general", destination=None, body="hello"
    )
    second, created2 = await repo.create_note(
        OWNER, client_id="idem-1", domain="general", destination=None, body="retry"
    )
    assert created1 and not created2
    assert first.id == second.id
    assert second.body == "hello"  # the retry never overwrites


async def test_unknown_domain_rejected_by_fk(repo: SqlNotesRepo) -> None:
    with pytest.raises(UnknownDomain):
        await repo.create_note(
            OWNER, client_id="bad-dom", domain="not-a-domain", destination=None, body="x"
        )


async def test_notes_domain_firewall(repo: SqlNotesRepo) -> None:
    await repo.create_note(
        OWNER, client_id="fw-gen", domain="general", destination=None, body="grocery run"
    )
    await repo.create_note(
        OWNER, client_id="fw-health", domain="health", destination="Labs", body="BP 118/76"
    )

    health_view = await repo.list_notes(HEALTH_ONLY, limit=50, before=None)
    assert {n.client_id for n in health_view} >= {"fw-health"}
    assert all(n.domain == "health" for n in health_view)

    general_view = await repo.list_notes(GENERAL_ONLY, limit=50, before=None)
    assert all(n.domain == "general" for n in general_view)

    assert await repo.list_notes(UNSCOPED, limit=50, before=None) == []

    owner_domains = {n.domain for n in await repo.list_notes(OWNER, limit=50, before=None)}
    assert {"general", "health"} <= owner_domains


async def test_scoped_writer_cannot_create_outside_its_domain(repo: SqlNotesRepo) -> None:
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        await repo.create_note(
            GENERAL_ONLY, client_id="sneak", domain="health", destination=None, body="sneaky"
        )


async def test_attachments_inherit_note_domain_and_firewall(repo: SqlNotesRepo) -> None:
    note, _ = await repo.create_note(
        OWNER, client_id="att-h", domain="health", destination="Labs", body="lab report"
    )
    attachment = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256="ab" * 32,
        filename="lab.pdf",
        media_type="application/pdf",
        size_bytes=9,
    )
    assert attachment is not None

    # Visible inside the health scope, invisible outside it.
    assert await repo.get_attachment(HEALTH_ONLY, attachment.id) is not None
    assert await repo.get_attachment(GENERAL_ONLY, attachment.id) is None
    assert await repo.get_attachment(UNSCOPED, attachment.id) is None

    # A scoped principal can't attach to a note it can't see.
    assert (
        await repo.add_attachment(
            GENERAL_ONLY,
            note_id=note.id,
            sha256="cd" * 32,
            filename="x.txt",
            media_type="text/plain",
            size_bytes=1,
        )
        is None
    )
