"""The guided-intake routes end to end against real Postgres.

Owner management lifecycle (mint → list → get → revoke) under an owner cookie, plus
the security edge the public surface turns on: a redeemed intake cookie is a NON-owner
principal, so it 403s every owner-only route (the cookie reaches no owner surface)."""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api import intake
from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.intake.repo import SqlIntakeRepo
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


def _app(maker: async_sessionmaker) -> FastAPI:
    app = FastAPI()
    app.include_router(intake.router, prefix="/api")
    app.state.auth_repo = SqlAuthRepo(maker)
    app.state.intake_repo = SqlIntakeRepo(maker)
    app.state.settings = Settings(secure_cookies=False)
    return app


async def _subject(maker: async_sessionmaker) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, 'S', 'person')"),
            {"i": sid},
        )
    return sid


def _mint_body(subject_id: str, **over: object) -> dict:
    body: dict = dict(
        subject_id=subject_id,
        domain_code="general",
        fields_brief="collect a phone number",
        max_runs=3,
        bind_on_first=False,
        ttl_hours=24.0,
    )
    body.update(over)
    return body


async def test_owner_link_lifecycle(maker: async_sessionmaker) -> None:
    sid = await _subject(maker)
    app = _app(maker)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id=str(uuid.uuid4()), kind="owner", label="owner"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        minted = await client.post("/api/intake/links", json=_mint_body(sid))
        assert minted.status_code == 201
        body = minted.json()
        link_id, secret = body["id"], body["secret"]
        assert secret  # shown once at mint

        listed = await client.get("/api/intake/links")
        assert [link["id"] for link in listed.json()] == [link_id]
        got = await client.get(f"/api/intake/links/{link_id}")
        assert got.json()["max_opens"] == 12  # defaulted to 4x max_runs
        # No sessions/submissions yet.
        assert (await client.get(f"/api/intake/links/{link_id}/sessions")).json() == []
        assert (await client.get(f"/api/intake/links/{link_id}/submissions")).json() == []

        assert (await client.delete(f"/api/intake/links/{link_id}")).status_code == 204
        assert (await client.delete(f"/api/intake/links/{link_id}")).status_code == 404
        assert (await client.get(f"/api/intake/links/{link_id}")).json()["status"] == "revoked"


async def test_mint_rejects_unknown_subject(maker: async_sessionmaker) -> None:
    app = _app(maker)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id=str(uuid.uuid4()), kind="owner", label="owner"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/api/intake/links", json=_mint_body(str(uuid.uuid4())))
        assert resp.status_code == 400


async def test_redeemed_intake_cookie_is_non_owner_and_403s_owner_routes(
    maker: async_sessionmaker,
) -> None:
    sid = await _subject(maker)
    # Mint a link directly (owner primitive), then drive the PUBLIC redeem route.
    ctx = SessionContext(principal_id=str(uuid.uuid4()), principal_kind="owner")
    from jbrain.intake import service

    secret, record = await service.mint_intake_link(
        SqlIntakeRepo(maker), ctx, service.IntakeLinkConfig(**_redeem_config(sid))
    )
    app = _app(maker)  # NO override: real cookie auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        redeemed = await client.post("/api/intake/redeem", json={"secret": secret})
        assert redeemed.status_code == 200
        assert redeemed.json()["link_id"] == record.id
        # The cookie max-age is capped at the link TTL, never the 30-day default.
        cookie = redeemed.headers["set-cookie"]
        assert "Max-Age=" in cookie
        max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
        assert 0 < max_age <= 24 * 3600

        # That same cookie (now in the jar) is a non-owner principal: every owner-only
        # intake route 403s it. The redeemed stranger reaches no owner surface.
        assert (await client.get("/api/intake/links")).status_code == 403
        assert (await client.post("/api/intake/links", json=_mint_body(sid))).status_code == 403
        # It does authenticate as the intake principal (401 would mean no cookie at all).
        who = await auth_service.authenticate(
            SqlAuthRepo(maker), client.cookies.get("jbrain_session") or ""
        )
        assert who is not None and who.kind == "intake_link"

        # Revoking the link kills the in-flight cookie at the ROUTE layer too: the next
        # request no longer authenticates, so current_principal 401s (cascade, end-to-end).
        from jbrain.intake import service as _svc

        assert await _svc.revoke_intake_link(SqlIntakeRepo(maker), ctx, record.id) is True
        assert (await client.get("/api/intake/links")).status_code == 401


async def test_redeem_rejects_bad_secret_with_no_cookie(maker: async_sessionmaker) -> None:
    """The security-path 401 branch: an invalid/expired/exhausted secret 401s and writes
    NO Set-Cookie header (CLAUDE.md 100% on the redeem path)."""
    app = _app(maker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/api/intake/redeem", json={"secret": "not-a-real-secret"})
        assert resp.status_code == 401
        assert "set-cookie" not in {k.lower() for k in resp.headers}
        assert client.cookies.get("jbrain_session") is None


def _redeem_config(subject_id: str) -> dict:
    return dict(
        subject_id=subject_id,
        domain_code="general",
        label="intake",
        persona_brief="",
        fields_brief="collect a phone number",
        opening_blurb="welcome",
        max_runs=3,
        max_opens=6,
        bind_on_first=False,
        ttl_hours=24.0,
    )
