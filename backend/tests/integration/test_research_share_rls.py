"""Migration 0150 against real Postgres: report share links are enforced in RLS, not app code.

The security keystone. A public visitor runs under `research_share_context` — a non-owner,
empty-scope principal pinned to one share-link id. The `research_reports_share` policy is the
ONLY grant it earns: it reads exactly the pinned link's target report (or its folder's current
members), and nothing else — not another report, not the external video corpus, not owner-only
folders, not a health note — with revocation re-checked at the data layer and no write anywhere.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, research_share_context, scoped_session
from jbrain.external.report_groups import create_report_group, set_report_group
from jbrain.external.research_shares import (
    fetch_shared_report,
    list_group_reports,
    list_shares,
    mint_share,
    resolve_share,
    revoke_share,
)
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
    # research_share_links.principal_id FKs to app.principals, so the owner ctx must carry a
    # REAL owner principal (like the report_groups test) — rotate a key, read it back.
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _insert_report(maker: async_sessionmaker, ctx: SessionContext, question: str) -> str:
    async with scoped_session(maker, ctx) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.research_reports"
                        " (question, question_hash, report_md, status, domain_code)"
                        " VALUES (:q, :h, 'body', 'done', 'external') RETURNING id"
                    ),
                    {"q": question, "h": uuid.uuid4().hex},
                )
            ).scalar_one()
        )


async def _visible_report_ids(maker: async_sessionmaker, link_id: str) -> set[str]:
    async with scoped_session(maker, research_share_context(link_id)) as s:
        rows = (await s.execute(text("SELECT id FROM app.research_reports"))).all()
    return {str(r.id) for r in rows}


async def test_report_link_reads_only_its_target(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid_a = await _insert_report(maker, owner, "How does rituximab work?")
    rid_b = await _insert_report(maker, owner, "How do mRNA vaccines work?")
    secret, link = await mint_share(
        maker, owner, target_kind="report", report_id=rid_a, group_id=None, label="A"
    )

    # The token resolves (public_read policy) to the live link; the read is pinned to it.
    resolved = await resolve_share(maker, secret)
    assert resolved is not None and resolved.report_id == rid_a
    got = await fetch_shared_report(maker, link.id, rid_a)
    assert got is not None and got.id == rid_a

    # RLS, not the WHERE clause, is the gate: pinned to link A the ONLY visible report is A.
    assert await _visible_report_ids(maker, link.id) == {rid_a}
    assert await fetch_shared_report(maker, link.id, rid_b) is None  # report B is out of scope


async def test_share_context_leaks_nothing_else(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid = await _insert_report(maker, owner, "sole shared report")
    _, link = await mint_share(
        maker, owner, target_kind="report", report_id=rid, group_id=None, label="A"
    )
    # Seed an external-corpus video, an owner-only folder, and a health note — none of which a
    # share visitor may see. The video is the key case: same `external` domain, so only the
    # EMPTY scope keeps it hidden.
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.external_sources"
                " (provider, video_id, url, title, channel_name, summary, transcript_source,"
                "  duration_s, status, analyzed_at)"
                " VALUES ('youtube', 'vid1', 'https://y', 't', 'c', 's', 'captions', 1,"
                "  'done', now())"
            )
        )
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'health', 'BP 118/76')"
            ),
            {"id": str(uuid.uuid4()), "cid": f"share-{uuid.uuid4().hex[:12]}"},
        )
    await create_report_group(maker, owner, name="Private")

    async with scoped_session(maker, research_share_context(link.id)) as s:
        for table in ("app.external_sources", "app.notes", "app.report_groups"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0, f"share context must not read {table}"


async def test_revoked_link_reads_nothing(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid = await _insert_report(maker, owner, "to be revoked")
    secret, link = await mint_share(
        maker, owner, target_kind="report", report_id=rid, group_id=None, label="A"
    )
    assert await revoke_share(maker, owner, link.id) is True

    assert await resolve_share(maker, secret) is None  # token dead
    assert await _visible_report_ids(maker, link.id) == set()  # policy re-checks revoked_at
    assert await fetch_shared_report(maker, link.id, rid) is None
    assert await revoke_share(maker, owner, link.id) is False  # already revoked → no-op


async def test_no_pin_and_write_denial(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid = await _insert_report(maker, owner, "report")
    _, link = await mint_share(
        maker, owner, target_kind="report", report_id=rid, group_id=None, label="A"
    )
    # An unpinned (empty principal_id) share context reads zero reports and does NOT raise —
    # the policy's nullif guard turns the empty pin into a NULL that matches nothing.
    assert await _visible_report_ids(maker, "") == set()

    # Provably read-only: an insert under the pinned share context is refused by RLS.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, research_share_context(link.id)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.research_share_links"
                    " (token_hash, target_kind, report_id, principal_id)"
                    " VALUES (:h, 'report', cast(:rid AS uuid), cast(:pid AS uuid))"
                ),
                {"h": uuid.uuid4().hex, "rid": rid, "pid": owner.principal_id},
            )


async def test_non_owner_cannot_list_share_rows(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    rid = await _insert_report(maker, owner, "report")
    _, link = await mint_share(
        maker, owner, target_kind="report", report_id=rid, group_id=None, label="A"
    )
    assert link.id in {s.id for s in await list_shares(maker, owner)}
    # A generic non-owner token (no research_share auth context) sees no share rows at all.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as s:
        n = (await s.execute(text("SELECT count(*) FROM app.research_share_links"))).scalar_one()
    assert n == 0


async def test_group_link_membership_is_dynamic(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    group = await create_report_group(maker, owner, name="Medical")
    rid_a = await _insert_report(maker, owner, "report A")
    rid_b = await _insert_report(maker, owner, "report B")
    rid_c = await _insert_report(maker, owner, "report C (ungrouped)")
    await set_report_group(maker, owner, rid_a, group.id)
    await set_report_group(maker, owner, rid_b, group.id)

    _, link = await mint_share(
        maker, owner, target_kind="group", report_id=None, group_id=group.id, label="Medical"
    )
    assert {r.id for r in await list_group_reports(maker, link.id)} == {rid_a, rid_b}

    # Membership is evaluated at read time: move B out, C in → the folder link tracks it live.
    await set_report_group(maker, owner, rid_b, None)
    await set_report_group(maker, owner, rid_c, group.id)
    assert {r.id for r in await list_group_reports(maker, link.id)} == {rid_a, rid_c}
    # A member is fetchable through the link; a non-member (B, now ungrouped) is not.
    assert await fetch_shared_report(maker, link.id, rid_c) is not None
    assert await fetch_shared_report(maker, link.id, rid_b) is None
