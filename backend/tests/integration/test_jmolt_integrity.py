"""jmolt's integrity watch against real Postgres (docs/plans/JMOLT_PLAN.md, W4).

The tamper watch (M21) and account-state surfacing (M22): a foreign post on the public
profile, or a platform suspension, must engage the kill (M6), revert autonomy to OFF
(M7), record the state, and notify the owner — deduped to the state transition. Moltbook
HTTP is faked; the settings + outbox are real.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
from jbrain.notify import Notification, NotifyBus
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


class _CaptureBus(NotifyBus):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Notification] = []

    def publish(self, note: Notification) -> None:
        self.sent.append(note)


def _watch(
    maker, store: FakeSettingsStore, handler, notify: NotifyBus | None = None
) -> JmoltIntegrity:
    client = MoltbookClient(_key, transport=httpx.MockTransport(handler))
    return JmoltIntegrity(maker=maker, client=client, settings_store=store, notify=notify)  # type: ignore[arg-type]


async def _publish_row(maker, pid: str, *, title: str, moltbook_id: str) -> None:
    async with scoped_session(maker, _jmolt(pid)) as s:
        row_id = await OutboxRepo().stage(
            s, pid, kind="post", payload={"submolt_name": "general", "title": title, "content": "x"}
        )
    assert row_id is not None  # no dedup_key on a post, so a fresh stage always returns an id
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(
            s, row_id, "published", moltbook_id=moltbook_id, published=True
        )


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
            200,
            json={"recentPosts": [{"id": "p_known", "title": "tide pools"}], "status": "active"},
        )

    state = await _watch(maker, store, handler).check()
    assert state == "ok"
    assert store.values.get("moltbook_killed", False) is False  # nothing paused


async def test_title_collision_no_longer_hides_a_foreign_post(maker: async_sessionmaker) -> None:
    # M1: matching is by platform id ONLY. An attacker who reuses a known title on a
    # different-id post no longer passes as accounted-for.
    pid = await _owner_pid(maker)
    await _publish_row(maker, pid, title="tide pools", moltbook_id="p_known")
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"recentPosts": [{"id": "p_evil", "title": "tide pools"}]},  # same title, new id
        )

    assert await _watch(maker, store, handler).check() == "tamper"
    assert store.values["moltbook_killed"] is True


async def test_foreign_comment_also_trips_tamper(maker: async_sessionmaker) -> None:
    # M1: a key leak that posts a COMMENT (not a post) is caught too.
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"recentPosts": [], "recentComments": [{"id": "c_foreign", "body": "spam"}]},
        )

    assert await _watch(maker, store, handler).check() == "tamper"
    assert store.values["moltbook_killed"] is True


async def test_published_comment_is_accounted_for(maker: async_sessionmaker) -> None:
    # A comment jmolt published through the outbox (its id recorded) is NOT tamper.
    pid = await _owner_pid(maker)
    async with scoped_session(maker, _jmolt(pid)) as s:
        row_id = await OutboxRepo().stage(
            s, pid, kind="comment", payload={"post_id": "p1", "content": "hi"}
        )
    assert row_id is not None  # no dedup_key here, so a fresh stage always returns an id
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, row_id, "published", moltbook_id="c_mine", published=True)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"recentComments": [{"id": "c_mine", "body": "hi"}], "status": "active"}
        )

    assert await _watch(maker, store, handler).check() == "ok"


@pytest.mark.parametrize("status", ["disabled", "deactivated", "locked", "terminated"])
async def test_suspension_synonyms_auto_pause(maker: async_sessionmaker, status: str) -> None:
    # L6: recognise the common disable synonyms, not just the literal "suspended".
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recentPosts": [], "status": status})

    assert await _watch(maker, store, handler).check() == "suspended"
    assert store.values["moltbook_killed"] is True


# ---- account-state surfacing (M22) ---------------------------------------


async def test_suspension_auto_pauses_and_reenforces_but_notifies_once(
    maker: async_sessionmaker,
) -> None:
    await _owner_pid(maker)
    store = FakeSettingsStore()
    store.values["moltbook_autonomy"] = True
    bus = _CaptureBus()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recentPosts": [], "status": "suspended"})

    watch = _watch(maker, store, handler, notify=bus)
    assert await watch.check() == "suspended"
    assert store.values["moltbook_killed"] is True  # lane + drip paused (M6)
    assert store.values["moltbook_autonomy"] is False  # switch reverted (M7)
    assert len(bus.sent) == 1  # notified on the transition
    # Security-critical: if the owner clears the kill while STILL suspended, the next tick
    # RE-engages it (a compromised/suspended account must not stay writable) — but does not
    # re-notify (dedup on the transition).
    store.values["moltbook_killed"] = False
    assert await watch.check() == "suspended"
    assert store.values["moltbook_killed"] is True  # re-engaged
    assert len(bus.sent) == 1  # still just the one notification


async def test_moderation_is_surfaced_but_not_auto_paused(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recentPosts": [], "status": "limited"})

    assert await _watch(maker, store, handler).check() == "moderated"
    assert store.values.get("moltbook_killed", False) is False  # a label never auto-pauses
    assert store.values["moltbook_account_state"] == "moderated"


async def test_platform_read_failure_leaves_state_untouched(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    store = FakeSettingsStore()
    store.values["moltbook_account_state"] = "ok"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    # A 5xx is a "cannot check", never a false tamper — the prior state stands.
    assert await _watch(maker, store, handler).check() == "ok"
    assert store.values.get("moltbook_killed", False) is False


async def test_a_healthy_pass_leaves_a_heartbeat(maker: async_sessionmaker) -> None:
    """C3 — the deadman. The watch wrote state ONLY on a transition, so a healthy pass wrote
    nothing and logged nothing. That made "healthy for days" and "never ran since deploy"
    the same observation, and on the live box `moltbook_account_state` did not exist at all:
    with full DB and log access there was no way to tell which one was true. The owner has no
    terminal, so "no alarms" has to mean something, and it only can if the watch says it ran.
    """
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent": {"username": "jmolt", "status": "active"}})

    assert await _watch(maker, store, handler).check() == "ok"
    assert store.values["moltbook_integrity_last_pass"]


async def test_the_heartbeat_records_a_pass_the_platform_broke(maker: async_sessionmaker) -> None:
    """Stamped before the platform read, deliberately: a watch that ran and could not reach
    Moltbook is alive, and that is exactly the fact the owner is missing. Recording only
    successful passes would make a permanently-unreachable platform look like a dead watch —
    the same ambiguity in a different place."""
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    await _watch(maker, store, handler).check()
    assert store.values["moltbook_integrity_last_pass"]


async def test_the_heartbeat_advances_on_every_pass(maker: async_sessionmaker) -> None:
    """It has to be an advancing timestamp, not a flag: "it ran once, months ago" is the
    state the deadman exists to make visible."""
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent": {"username": "jmolt", "status": "active"}})

    watch = _watch(maker, store, handler)
    await watch.check(now=datetime(2026, 8, 27, 3, 0, tzinfo=UTC))
    first = store.values["moltbook_integrity_last_pass"]
    await watch.check(now=datetime(2026, 8, 27, 4, 0, tzinfo=UTC))

    assert store.values["moltbook_integrity_last_pass"] != first
