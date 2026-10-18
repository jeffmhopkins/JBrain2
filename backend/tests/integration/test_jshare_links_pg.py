"""JShare's all-links list against real Postgres: one link of every kind is minted through its
own feature, shows up in the one list with a readable title, and is revoked through it, after
which the feature's own list no longer has it."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import chat_shares
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.external import jlaunch_shares, research_shares
from jbrain.intake import service as intake_service
from jbrain.intake.repo import SqlIntakeRepo
from jbrain.intake.service import IntakeLinkConfig
from jbrain.jshare import links
from jbrain.minecraft import shares as mc_shares
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


async def _exec(maker: async_sessionmaker, ctx: SessionContext, sql: str, **params: object):
    async with scoped_session(maker, ctx) as s:
        result = await s.execute(text(sql), params)
        return result.first() if "RETURNING" in sql else None


async def _mint_one_of_each(maker: async_sessionmaker, owner: SessionContext) -> dict[str, str]:
    """Each kind's link id, minted through that feature's own code."""
    chat = str(uuid.uuid4())
    await _exec(
        maker,
        owner,
        "INSERT INTO app.agent_sessions (id, principal_id, title, status, domain_scopes,"
        " subject_ids) VALUES (cast(:id AS uuid), cast(:pid AS uuid), 'Trip plans', 'active',"
        " ARRAY['general'], ARRAY[]::uuid[])",
        id=chat,
        pid=owner.principal_id,
    )
    minted = await chat_shares.mint_share(maker, owner, chat)
    assert minted is not None

    report = await _exec(
        maker,
        owner,
        "INSERT INTO app.research_reports (question, question_hash, report_md, status,"
        " domain_code) VALUES ('Why is the sky blue?', :h, 'body', 'done', 'external')"
        " RETURNING id",
        h=uuid.uuid4().hex,
    )
    assert report is not None
    _, research = await research_shares.mint_share(
        maker, owner, target_kind="report", report_id=str(report.id), group_id=None, label=None
    )

    _, mc = await mc_shares.mint_share(
        maker, owner, slot="slot2", world="castle", xuid="x1", gamertag="BlockyFox"
    )

    run = f"run-{uuid.uuid4().hex[:8]}"
    await _exec(
        maker,
        owner,
        "INSERT INTO app.jlaunch_runs (id, spec_name, title, state, verify_ok)"
        " VALUES (:r, 'spec', 'census', 'succeeded', true)",
        r=run,
    )
    _, jl = await jlaunch_shares.mint_share(maker, owner, run_id=run, label=None)

    _, jcode = await auth_service.mint_jcode_share(
        SqlAuthRepo(maker), "sess-a", "for Sam", ttl_hours=24
    )

    subject = str(uuid.uuid4())
    await _exec(
        maker,
        owner,
        "INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, 'S', 'person')",
        i=subject,
    )
    _, intake = await intake_service.mint_intake_link(
        SqlIntakeRepo(maker),
        owner,
        IntakeLinkConfig(
            subject_id=subject,
            domain_code="general",
            label="phone number",
            persona_brief="",
            fields_brief="collect a phone number",
            opening_blurb="hi",
            max_runs=5,
            max_opens=5,
            bind_on_first=False,
            ttl_hours=24.0,
        ),
    )
    return {
        "chat": minted[1].id,
        "research": research.id,
        "minecraft": mc.id,
        "jlaunch": jl.id,
        "jcode": jcode.id,
        "intake": intake.id,
    }


async def test_every_kind_is_listed_and_revoked_in_one_place(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    ids = await _mint_one_of_each(maker, owner)
    revoked_jcode: list[tuple[str, str]] = []
    auth = SqlAuthRepo(maker)

    async def revoke_jcode(sid: str, share_id: str) -> bool:
        revoked_jcode.append((sid, share_id))
        return await auth.revoke_jcode_share(share_id, sid)

    src = links.Sources(
        maker=maker, auth_repo=auth, intake_repo=SqlIntakeRepo(maker), revoke_jcode=revoke_jcode
    )

    found, failed = await links.list_links(src, owner)
    assert failed == []
    by_kind = {link.kind: link for link in found}
    assert {k: by_kind[k].id for k in ids} == ids
    assert by_kind["chat"].title == "Trip plans"
    assert by_kind["research"].title == "Why is the sky blue?"
    assert by_kind["minecraft"].title == "castle map"
    assert by_kind["jlaunch"].title == "census"
    assert by_kind["jcode"].title == "for Sam"
    assert by_kind["intake"].title == "phone number"

    for kind, link_id in ids.items():
        assert await links.revoke(src, owner, kind, link_id), kind
        assert not await links.revoke(src, owner, kind, link_id), f"{kind} revoked twice"

    assert await links.list_links(src, owner) == ([], [])
    # jcode went through the route's own revoke, with the link's session.
    assert revoked_jcode == [("sess-a", ids["jcode"])]
    # And each feature's own list agrees.
    assert await mc_shares.list_shares(maker, owner) == []
    assert await research_shares.list_shares(maker, owner) == []
    assert await auth.list_jcode_shares("sess-a") == []


async def test_unknown_kinds_and_ids_revoke_nothing(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    auth = SqlAuthRepo(maker)

    async def never(_sid: str, _share_id: str) -> bool:
        raise AssertionError("no jcode link should reach the revoker")

    src = links.Sources(
        maker=maker, auth_repo=auth, intake_repo=SqlIntakeRepo(maker), revoke_jcode=never
    )
    assert not await links.revoke(src, owner, "nope", str(uuid.uuid4()))
    for kind in links.KINDS:
        assert not await links.revoke(src, owner, kind, "not-a-uuid"), kind
        assert not await links.revoke(src, owner, kind, str(uuid.uuid4())), kind


async def test_the_jcode_kind_revokes_only_jcode_links(maker: async_sessionmaker) -> None:
    """Every link here is a principal row; the jcode kind must not reach another kind's."""
    owner = await _owner_ctx(maker)
    ids = await _mint_one_of_each(maker, owner)
    auth = SqlAuthRepo(maker)

    async def never(_sid: str, _share_id: str) -> bool:
        raise AssertionError("only a jcode link should reach the revoker")

    src = links.Sources(
        maker=maker, auth_repo=auth, intake_repo=SqlIntakeRepo(maker), revoke_jcode=never
    )
    for kind in ("intake", "chat"):
        assert not await links.revoke(src, owner, "jcode", ids[kind]), kind
    assert not await links.revoke(src, owner, "jcode", str(owner.principal_id))
    # The module shares one database, so check this test's own links, not the whole list.
    found, _ = await links.list_links(src, owner)
    assert set(ids.values()) <= {link.id for link in found}
