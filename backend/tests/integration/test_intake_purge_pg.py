"""The intake purge cascade (W4, real Postgres).

Purging a link removes the link → its sessions → its submissions → the retained
transcripts (ON DELETE CASCADE), and soft-deletes the approved-and-ingested derived
notes (by source_ref) so their facts are reaped by the standard deleted-note artifact
purge (#11/§5)."""

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.intake import service
from jbrain.intake.repo import SqlIntakeRepo
from jbrain.intake.service import IntakeLinkConfig
from jbrain.notes.repo import SqlNotesRepo
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


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    await auth_service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_purge_cascades_link_sessions_submissions_and_derived_notes(
    maker: async_sessionmaker,
) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlIntakeRepo(maker)

    # A subject, a link, a redeemed session, a captured submission, and an approved
    # derived note (provenance untrusted_origin, attributed to the submission).
    subject_id = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, 'S', 'person')"),
            {"i": subject_id},
        )
    secret, link = await service.mint_intake_link(
        repo,
        ctx,
        IntakeLinkConfig(
            subject_id=subject_id,
            domain_code="general",
            label="intake",
            persona_brief="",
            fields_brief="x",
            opening_blurb="hi",
            max_runs=2,
            max_opens=5,
            bind_on_first=False,
            ttl_hours=24.0,
        ),
    )
    claim = await repo.claim(
        secret_hash=keys.hash_token(secret),
        principal_key_hash=keys.hash_token(uuid.uuid4().hex),
        label="x",
    )
    assert claim is not None
    sub_id = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.intake_submissions"
                " (id, link_id, session_id, principal_id, transcript, status)"
                " VALUES (:i, :l, :s, :p, CAST(:t AS jsonb), 'submitted')"
            ),
            {
                "i": sub_id,
                "l": link.id,
                "s": claim.session_id,
                "p": claim.principal_id,
                "t": json.dumps([{"role": "recipient", "text": "555"}]),
            },
        )
    note, _ = await SqlNotesRepo(maker).create_note(
        ctx,
        client_id=f"intake-{uuid.uuid4()}",
        domain="general",
        destination=None,
        body="Phone is 555.",
        provenance="untrusted_origin",
        source_ref=f"intake-submission:{sub_id}",
    )

    retired = await repo.purge_intake_link(ctx, link.id)
    assert retired == 1  # the derived note was soft-deleted

    async with scoped_session(maker, ctx) as session:
        # The link and everything cascading from it are gone.
        for query in (
            "SELECT count(*) FROM app.intake_links WHERE id = :i",
            "SELECT count(*) FROM app.intake_sessions WHERE link_id = :i",
            "SELECT count(*) FROM app.intake_submissions WHERE link_id = :i",
        ):
            assert (await session.execute(text(query), {"i": link.id})).scalar() == 0, query
        # The derived note is soft-deleted (its facts are reaped by the artifact purge).
        deleted_at = (
            await session.execute(
                text("SELECT deleted_at FROM app.notes WHERE id = :i"), {"i": note.id}
            )
        ).scalar()
    assert deleted_at is not None
