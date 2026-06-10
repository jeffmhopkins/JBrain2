"""Notes/attachments against real Postgres: RLS isolation per CLAUDE.md rule 3,
plus the idempotency and FK behaviors the unit fakes can only imitate."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import NoteUpdate, UnknownDomain
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
def repo(maker: async_sessionmaker) -> SqlNotesRepo:
    return SqlNotesRepo(maker)


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


async def test_update_note_edits_fields_and_resets_ingest_state(repo: SqlNotesRepo) -> None:
    note, _ = await repo.create_note(
        OWNER, client_id="up-1", domain="general", destination="Inbox", body="draft"
    )
    updated = await repo.update_note(
        OWNER, note.id, NoteUpdate(body="final", destination="Journal")
    )
    assert updated is not None
    assert updated.body == "final"
    assert updated.destination == "Journal"
    assert updated.ingest_state == "pending"  # edit always re-queues indexing

    cleared = await repo.update_note(OWNER, note.id, NoteUpdate(clear_destination=True))
    assert cleared is not None and cleared.destination is None


async def test_update_note_moves_domain_with_attachments(
    repo: SqlNotesRepo, maker: async_sessionmaker
) -> None:
    note, _ = await repo.create_note(
        OWNER, client_id="up-mv", domain="general", destination=None, body="actually medical"
    )
    attachment = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256="ef" * 32,
        filename="scan.pdf",
        media_type="application/pdf",
        size_bytes=3,
    )
    assert attachment is not None

    moved = await repo.update_note(OWNER, note.id, NoteUpdate(domain="health"))
    assert moved is not None and moved.domain == "health"
    # The attachment crossed the firewall with its note: general scope lost
    # it, health scope gained it. Leaving it behind would leak health data.
    assert await repo.get_attachment(GENERAL_ONLY, attachment.id) is None
    assert await repo.get_attachment(HEALTH_ONLY, attachment.id) is not None


async def test_update_note_rls_and_unknown_domain(repo: SqlNotesRepo) -> None:
    note, _ = await repo.create_note(
        OWNER, client_id="up-rls", domain="health", destination=None, body="BP note"
    )
    # Invisible under the wrong scope: indistinguishable from missing.
    assert await repo.update_note(GENERAL_ONLY, note.id, NoteUpdate(body="hacked")) is None
    with pytest.raises(UnknownDomain):
        await repo.update_note(OWNER, note.id, NoteUpdate(domain="not-a-domain"))

    # A scoped writer cannot push its own note across the firewall either.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await repo.update_note(HEALTH_ONLY, note.id, NoteUpdate(domain="general"))


async def test_delete_note_soft_deletes_and_hard_deletes_chunks(
    repo: SqlNotesRepo, maker: async_sessionmaker
) -> None:
    note, _ = await repo.create_note(
        OWNER, client_id="del-1", domain="general", destination=None, body="goodbye"
    )
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (gen_random_uuid(), :nid, 'general', 'paragraph', 0, 'goodbye')"
            ),
            {"nid": note.id},
        )

    assert await repo.delete_note(OWNER, note.id) is True
    assert all(n.id != note.id for n in await repo.list_notes(OWNER, limit=50, before=None))
    async with scoped_session(maker, OWNER) as session:
        deleted_at = (
            await session.execute(
                text("SELECT deleted_at FROM app.notes WHERE id = :id"), {"id": note.id}
            )
        ).scalar()
        chunks = (
            await session.execute(
                text("SELECT count(*) FROM app.chunks WHERE note_id = :id"), {"id": note.id}
            )
        ).scalar()
    assert deleted_at is not None  # the source of truth survives, soft-deleted
    assert chunks == 0  # the search index forgets immediately

    # Idempotence boundary: an already-deleted note reads as missing.
    assert await repo.delete_note(OWNER, note.id) is False


async def test_scoped_principal_cannot_delete_across_domains(repo: SqlNotesRepo) -> None:
    note, _ = await repo.create_note(
        OWNER, client_id="del-rls", domain="health", destination=None, body="protected"
    )
    assert await repo.delete_note(GENERAL_ONLY, note.id) is False
    assert any(n.id == note.id for n in await repo.list_notes(OWNER, limit=50, before=None))


async def test_location_fields_roundtrip(repo: SqlNotesRepo) -> None:
    note, _ = await repo.create_note(
        OWNER,
        client_id="loc-pg",
        domain="general",
        destination=None,
        body="captured outside",
        latitude=47.6097,
        longitude=-122.3331,
        accuracy_m=8.0,
    )
    listed = next(n for n in await repo.list_notes(OWNER, limit=50, before=None) if n.id == note.id)
    assert (listed.latitude, listed.longitude, listed.accuracy_m) == (47.6097, -122.3331, 8.0)


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
