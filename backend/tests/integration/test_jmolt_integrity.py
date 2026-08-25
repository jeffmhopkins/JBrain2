"""jmolt's integrity watch against real Postgres (docs/plans/JMOLT_PLAN.md, W4).

The tamper watch (M21) and account-state surfacing (M22): a foreign post on the public
profile, or a platform suspension, must engage the kill (M6), revert autonomy to OFF
(M7), record the state, and notify the owner — deduped to the state transition. Moltbook
HTTP is faked; the settings + outbox are real.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_integrity import JmoltIntegrity, classify_account
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import OutboxRepo
from jbrain.web.moltbook import MoltbookClient
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
    yield


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


async def _key() -> tuple[str, str]:
    return "moltbook_key123456", "jmolt"


def _watch(maker, store: FakeSettingsStore, handler) -> JmoltIntegrity:
    client = MoltbookClient(_key, transport=httpx.MockTransport(handler))
    return JmoltIntegrity(maker=maker, client=client, settings_store=store)


async def _publish_row(maker, pid: str, *, title: str, moltbook_id: str) -> None:
    async with scoped_session(maker, _jmolt(pid)) as s:
        row_id = await OutboxRepo().stage(
            s, pid, kind="post", payload={"submolt_name": "general", "title": title, "content": "x"}
        )
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, row_id, "published", moltbook_id=moltbook_id, published=True)


# ---- classify_account (pure) ---------------------------------------------


@pytest.mark.parametrize(
    "me,expected",
    [
        ({"status": "active"}, "ok"),
        ({"status": "suspended"}, "suspended"),
        ({"banned": True}, "suspended"),
        ({"status": "limited"}, "moderated"),
        ({"labels": ["spam"]}, "moderated"),
        ({"rate_limited": True}, "moderated"),
        ({}, "ok"),
    ],
)
def test_classify_account_shapes(me: dict, expected: str) -> None:
    assert classify_account(me) == expected


# ---- tamper watch (M21) --------------------------------------------------


async def test_foreign_post_trips_tamper_and_engages_the_kill(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    # jmolt legitimately published one post (id p_known); the profile shows a SECOND post
    # (id p_foreign) that never went through the outbox → a key leak.
    await _publish_row(maker, pid, title="tide pools", moltbook_id="p_known")
    store = FakeSettingsStore()
    store.values["moltbook_autonomy"] = True

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "recentPosts": [
                    {"id": "p_known", "title": "tide pools"},
                    {"id": "p_foreign", "title": "buy my token"},
                ]
            },
        )

    state = await _watch(maker, store, handler).check()
    assert state == "tamper"
    assert store.values["moltbook_killed"] is True  # M6 engaged
    assert store.values["moltbook_autonomy"] is False  # M7 auto-reverted
    assert store.values["moltbook_account_state"] == "tamper"


async def test_all_profile_posts_accounted_for_is_ok(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _publish_row(maker, pid, title="tide pools", moltbook_id="p_known")
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"recentPosts": [{"id": "p_known", "title": "tide pools"}], "status": "active"}
        )

    state = await _watch(maker, store, handler).check()
    assert state == "ok"
    assert store.values.get("moltbook_killed", False) is False  # nothing paused


# ---- account-state surfacing (M22) ---------------------------------------


async def test_suspension_auto_pauses_and_notifies_once(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()
    store.values["moltbook_autonomy"] = True

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recentPosts": [], "status": "suspended"})

    watch = _watch(maker, store, handler)
    assert await watch.check() == "suspended"
    assert store.values["moltbook_killed"] is True  # lane + drip paused (M6)
    assert store.values["moltbook_autonomy"] is False  # switch reverted (M7)
    # Deduped: a second identical tick makes no further change (still suspended).
    store.values["moltbook_killed"] = False  # pretend the owner cleared it
    assert await watch.check() == "suspended"
    assert store.values["moltbook_killed"] is False  # no re-engage on the SAME state


async def test_moderation_is_surfaced_but_not_auto_paused(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recentPosts": [], "status": "limited"})

    assert await _watch(maker, store, handler).check() == "moderated"
    assert store.values.get("moltbook_killed", False) is False  # a label never auto-pauses
    assert store.values["moltbook_account_state"] == "moderated"


async def test_platform_read_failure_leaves_state_untouched(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()
    store.values["moltbook_account_state"] = "ok"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    # A 5xx is a "cannot check", never a false tamper — the prior state stands.
    assert await _watch(maker, store, handler).check() == "ok"
    assert store.values.get("moltbook_killed", False) is False
