"""The chat share-link API end-to-end against real Postgres (migration 0238).

Drives the actual FastAPI app: the owner mints a link to a chat from the sessions router (secret
shown once), then an UNAUTHENTICATED client reads it at the public `/chat-share` surface —
proving the no-login grant serves the owner's own transcript shape, that a turn added later
shows on the next load, that revocation and unknown tokens 404, that the anti-leak headers are
set, and that the share's favicon route only fetches hosts the chat actually reached.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.locations.ratelimit import TokenBucket
from jbrain.main import create_app
from jbrain.web.favicon import FaviconResult
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _Fetcher:
    """Stands in for the SSRF-guarded favicon fetcher: records which hosts were asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def fetch(self, host: str) -> FaviconResult | None:
        self.asked.append(host)
        return FaviconResult(data=b"\x89PNG", content_type="image/png")


async def _owner_ctx(maker: async_sessionmaker[AsyncSession]) -> SessionContext:
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


_STEP = {
    "id": "t1",
    "name": "web_search",
    "ok": True,
    "args": {"query": "spacex secondary"},
    "web_sources": [{"url": "https://www.cnbc.com/spacex", "title": "CNBC"}],
}


async def test_chat_share_api_end_to_end(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        # Owner-gated management: no session -> 401.
        assert client.post("/api/sessions/x/shares").status_code == 401
        assert client.get("/api/sessions/x/shares").status_code == 401
        assert client.delete("/api/sessions/shares/x").status_code == 401

        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204
        sid = client.post(
            "/api/sessions",
            json={"domain_scopes": ["general", "finance"], "title": "SpaceX Pretrade"},
        ).json()["id"]
        owner = await _owner_ctx(maker)
        transcript = AgentTranscript(maker)
        await transcript.record_exchange(
            owner,
            session_id=sid,
            run_id=None,
            user_text="Where is it trading?",
            assistant_text="Above the last tender[^1].",
            tools=[_STEP],
            reasoning="search first",
        )

        assert client.post("/api/sessions/not-a-session/shares").status_code == 404
        assert client.get("/api/sessions/not-a-session/shares/sensitivity").status_code == 404
        # The chat can read finance, so the owner confirms before a link exists.
        assert client.get(f"/api/sessions/{sid}/shares/sensitivity").json() == {
            "sensitive": ["finance"]
        }
        refused = client.post(f"/api/sessions/{sid}/shares")
        assert refused.status_code == 409
        assert refused.json()["detail"]["sensitive"] == ["finance"]
        assert client.get(f"/api/sessions/{sid}/shares").json() == []
        minted = client.post(f"/api/sessions/{sid}/shares", json={"confirm_sensitive": True})
        assert minted.status_code == 201
        body = minted.json()
        token, share_id = body["token"], body["id"]
        assert token and body["view_count"] == 0

        listed = client.get(f"/api/sessions/{sid}/shares").json()
        assert [s["id"] for s in listed] == [share_id]
        assert "token" not in listed[0]  # the secret is never listed
        (row,) = [s for s in client.get("/api/sessions").json() if s["id"] == sid]
        assert row["shared"] is True
        owner_turns = client.get(f"/api/sessions/{sid}/transcript").json()

        # The PUBLIC read, with no cookie at all.
        fetcher = _Fetcher()
        app.state.favicon_fetcher = fetcher
        client.cookies.clear()
        public = client
        page = public.get(f"/api/chat-share/{token}")
        assert page.status_code == 200
        assert page.headers["Referrer-Policy"] == "no-referrer"
        assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert page.headers["Cache-Control"] == "no-store"
        shared = page.json()
        assert shared["title"] == "SpaceX Pretrade"
        assert shared["agent"] == "curator"
        # The same turn shape the owner's reopen replays.
        assert shared["turns"] == owner_turns

        # A turn the owner adds later shows on the visitor's next load.
        await transcript.record_answer(
            owner, session_id=sid, run_id=None, assistant_text="A follow-up.", tools=[]
        )
        later = public.get(f"/api/chat-share/{token}").json()
        assert [t["content"] for t in later["turns"]][-1] == "A follow-up."

        # Favicons: a host the chat reached is served (either spelling); any other 404s
        # without the box fetching it.
        assert public.get(f"/api/chat-share/{token}/favicon?host=cnbc.com").status_code == 200
        assert public.get(f"/api/chat-share/{token}/favicon?host=www.cnbc.com").status_code == 200
        assert public.get(f"/api/chat-share/{token}/favicon?host=evil.test").status_code == 404
        assert public.get(f"/api/chat-share/{token}/favicon?host=").status_code == 404
        assert public.get("/api/chat-share/nope/favicon?host=cnbc.com").status_code == 404
        assert "evil.test" not in fetcher.asked

        assert public.get("/api/chat-share/unknown-token").status_code == 404
        # The public router never opens owner routes.
        assert public.get(f"/api/sessions/{sid}/transcript").status_code in (401, 403)

        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204
        # Two page loads counted; favicon lookups are not views.
        assert client.get(f"/api/sessions/{sid}/shares").json()[0]["view_count"] == 2

        assert client.delete(f"/api/sessions/shares/{share_id}").status_code == 204
        assert client.delete(f"/api/sessions/shares/{share_id}").status_code == 404
        assert client.get(f"/api/sessions/{sid}/shares").json() == []
        assert client.get(f"/api/chat-share/{token}").status_code == 404


async def test_chat_share_mint_confirms_private_reads(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A harmless chat mints from a bare POST as before; a chat re-scoped to `general` after a
    turn quoted a health note, or one that read mail, needs the owner's confirm."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    owner = await _owner_ctx(maker)
    transcript = AgentTranscript(maker)
    with TestClient(app) as client:
        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204

        def chat(scopes: list[str]) -> str:
            return str(client.post("/api/sessions", json={"domain_scopes": scopes}).json()["id"])

        harmless = chat(["general"])
        await transcript.record_exchange(
            owner,
            session_id=harmless,
            run_id=None,
            user_text="news?",
            assistant_text="Here.",
            tools=[_STEP],
        )
        assert client.get(f"/api/sessions/{harmless}/shares/sensitivity").json() == {
            "sensitive": []
        }
        assert client.post(f"/api/sessions/{harmless}/shares").status_code == 201

        rescoped = chat(["general", "health"])
        health_read = {
            "id": "t2",
            "name": "search",
            "ok": True,
            "sources": [{"note_id": "n1", "domain": "health", "snippet": "A1c 6.1"}],
        }
        await transcript.record_exchange(
            owner,
            session_id=rescoped,
            run_id=None,
            user_text="my A1c?",
            assistant_text="6.1",
            tools=[health_read],
        )
        assert (
            client.post(f"/api/sessions/{rescoped}/scope", json={"domain_scopes": ["general"]})
        ).status_code == 204
        refused = client.post(f"/api/sessions/{rescoped}/shares", json={})
        assert refused.status_code == 409
        assert refused.json()["detail"]["sensitive"] == ["health"]
        ok = client.post(f"/api/sessions/{rescoped}/shares", json={"confirm_sensitive": True})
        assert ok.status_code == 201
        # The confirmed link serves the chat in full: the owner chose to share it.
        client.cookies.clear()
        page = client.get(f"/api/chat-share/{ok.json()['token']}").json()
        assert page["turns"][1]["tools"][0]["sources"][0]["snippet"] == "A1c 6.1"

        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204
        mail = chat(["general"])
        await transcript.record_exchange(
            owner,
            session_id=mail,
            run_id=None,
            user_text="any bank mail?",
            assistant_text="One.",
            tools=[{"id": "t3", "name": "gmail_search", "ok": True, "summary": "From: bank"}],
        )
        assert client.post(f"/api/sessions/{mail}/shares").status_code == 409
        assert client.get(f"/api/sessions/{mail}/shares/sensitivity").json() == {
            "sensitive": ["mail"]
        }


async def test_chat_share_reads_are_rate_limited(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as public:
        app.state.chat_share_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
        app.state.chat_share_favicon_rate_limiter = TokenBucket(capacity=0, refill_per_sec=0.0)
        assert public.get("/api/chat-share/t").status_code == 404
        assert public.get("/api/chat-share/t").status_code == 429
        assert public.get("/api/chat-share/t/favicon?host=a.com").status_code == 429
