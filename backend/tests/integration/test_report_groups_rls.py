"""Migration 0149 against real Postgres: report_groups is owner-only metadata (CLAUDE.md
rule 3), like the task_groups it mirrors. Filing a report writes its opaque `group_id`
under the owner context, and deleting a folder SET NULLs its reports (they fall to
Ungrouped) rather than dropping them.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.external.report_groups import (
    create_report_group,
    delete_report_group,
    list_report_groups,
    rename_report_group,
    report_group_exists,
    set_report_group,
)
from jbrain.external.research_corpus import list_reports, rename_report
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


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    # The report_groups.principal_id FKs to app.principals, so the owner ctx must carry a
    # REAL owner principal (unlike test_rls.OWNER's placeholder id) — rotate a key to mint
    # it, then read it back.
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _insert_report(maker: async_sessionmaker, ctx: SessionContext) -> str:
    # A full-owner ctx passes the external-domain WITH CHECK, so it can seed a corpus row
    # directly (no queue/embed side-effects, unlike persist_report).
    async with scoped_session(maker, ctx) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.research_reports"
                        " (question, question_hash, report_md, status, domain_code)"
                        " VALUES ('How does rituximab work?', :h, 'body', 'done', 'external')"
                        " RETURNING id"
                    ),
                    {"h": uuid.uuid4().hex},
                )
            ).scalar_one()
        )


async def test_report_groups_are_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    g = await create_report_group(maker, owner, name="Medical")
    assert g.position == 0  # first folder anchors the order
    g2 = await create_report_group(maker, owner, name="Synths")
    assert g2.position == 1  # each new folder appends

    assert [x.name for x in await list_report_groups(maker, owner)] == ["Medical", "Synths"]
    # A non-owner principal sees no folders at all (RLS).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await list_report_groups(maker, token) == []
    # A narrowed owner still sees its folders — owner_scoped restricts domain data only.
    narrowed = SessionContext(
        principal_id=owner.principal_id,
        principal_kind="owner",
        domain_scopes=("external",),
        owner_scoped=True,
    )
    assert len(await list_report_groups(maker, narrowed)) == 2

    renamed = await rename_report_group(maker, owner, g.id, name="Health")
    assert renamed is not None and renamed.name == "Health"
    # existence guard: the owner's folder yes; a stranger uuid / a non-uuid, no.
    assert await report_group_exists(maker, owner, g.id) is True
    assert await report_group_exists(maker, owner, str(uuid.uuid4())) is False
    assert await report_group_exists(maker, owner, "not-a-uuid") is False


async def test_moving_a_report_files_it_and_delete_ungroups(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid = await _insert_report(maker, owner)
    g = await create_report_group(maker, owner, name="Medical")

    assert await set_report_group(maker, owner, rid, g.id) is True
    reports, _ = await list_reports(maker, limit=50, principal_id=owner.principal_id)
    assert {r.id: r.group_id for r in reports}[rid] == g.id  # filed, and the listing carries it

    # Deleting a folder must never lose reports: the FK SET NULLs them back to Ungrouped.
    await delete_report_group(maker, owner, g.id)
    reports2, _ = await list_reports(maker, limit=50, principal_id=owner.principal_id)
    assert {r.id: r.group_id for r in reports2}[rid] is None  # ungrouped, not deleted

    # Moving a report that isn't there is a harmless no-op (idempotent).
    assert await set_report_group(maker, owner, str(uuid.uuid4()), None) is False


async def test_renaming_a_report_sets_the_display_title(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid = await _insert_report(maker, owner)

    assert await rename_report(maker, owner, rid, "Rituximab mechanism") is True
    reports, _ = await list_reports(maker, limit=50, principal_id=owner.principal_id)
    assert {r.id: r.title for r in reports}[rid] == "Rituximab mechanism"

    # No `title IS NULL` guard: a second rename overrides the first (owner has final say).
    assert await rename_report(maker, owner, rid, "How rituximab works") is True
    reports2, _ = await list_reports(maker, limit=50, principal_id=owner.principal_id)
    assert {r.id: r.title for r in reports2}[rid] == "How rituximab works"

    # A principal without external-domain scope can't see or touch the row — RLS drops it
    # from its write scope, so nothing updates (the endpoint itself is owner-gated, but the
    # corpus write must not leak across the domain firewall either).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await rename_report(maker, token, rid, "Injected title") is False
    reports3, _ = await list_reports(maker, limit=50, principal_id=owner.principal_id)
    assert {r.id: r.title for r in reports3}[rid] == "How rituximab works"

    # Renaming a report that isn't there is a harmless no-op (idempotent).
    assert await rename_report(maker, owner, str(uuid.uuid4()), "Ghost") is False
