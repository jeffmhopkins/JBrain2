"""Long-lived WebSockets re-check their session (security plan S3.1, F10).

The live map, the jcode terminal and the jlaunch terminal authenticate on the handshake
and then re-resolve the same session token on a tick; a revoked or lapsed session closes
the socket 4401. The tick is shortened per app here (`ws_reauth_interval_s`) so a test
waits milliseconds, not the production 30 s.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode_terminal, jlaunch_terminal
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.jcode.client import FakeJcodeClient
from jbrain.locations.live import LiveBroadcaster, LiveFix
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeViewScopeRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_COOKIE = "jbrain_session"
_TICK = 0.05


def _fix(subject_id: str) -> LiveFix:
    return LiveFix(
        subject_id=subject_id,
        latitude=40.0,
        longitude=-74.0,
        accuracy_m=5.0,
        battery_pct=50,
        velocity_mps=0.0,
        captured_at=datetime(2026, 10, 12, tzinfo=UTC),
    )


class _Sink:
    async def record_view(self, ctx: SessionContext, **_: Any) -> None:
        return None


class _EchoUpstream:
    """The control server's PTY socket, echoing every frame back."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | str] = asyncio.Queue()
        self.closed = False

    async def send(self, data: bytes | str) -> None:
        await self.queue.put(data)

    def __aiter__(self) -> "_EchoUpstream":
        return self

    async def __anext__(self) -> bytes | str:
        return await self.queue.get()


class _FakeConnect:
    def __init__(self) -> None:
        self.upstreams: list[_EchoUpstream] = []

    def __call__(self, *_: Any, **__: Any) -> Any:
        @asynccontextmanager
        async def cm() -> AsyncIterator[_EchoUpstream]:
            upstream = _EchoUpstream()
            self.upstreams.append(upstream)
            try:
                yield upstream
            finally:
                upstream.closed = True

        return cm()


def _app(**overrides: Any) -> FastAPI:
    return create_app(
        Settings(secure_cookies=False, database_url=_DB, session_cookie=_COOKIE, **overrides)
    )


def _owner_token(repo: FakeAuthRepo) -> str:
    key = asyncio.run(auth_service.rotate_owner_key(repo))
    return asyncio.run(auth_service.login(repo, key, "pwa"))


def _member_token(repo: FakeAuthRepo, subject_id: str) -> tuple[str, str]:
    key = "device-key-" + subject_id
    asyncio.run(repo.create_principal("device_key", keys.hash_key(key), "phone", subject_id))
    token = asyncio.run(auth_service.mint_dashboard_session(repo, key))
    assert token is not None
    return token, repo.principals[-1].id


def _revoke_principal(repo: FakeAuthRepo, principal_id: str) -> None:
    for p in repo.principals:
        if p.id == principal_id:
            p.revoked = True


def _closed_with(ws: Any) -> int | None:
    message = ws.receive()
    assert message["type"] == "websocket.close", message
    return message.get("code")


# --- live location feed --------------------------------------------------------------


@pytest.fixture
def live() -> Iterator[tuple[FastAPI, TestClient, FakeAuthRepo, FakeViewScopeRepo]]:
    app = _app()
    repo = FakeAuthRepo()
    scope = FakeViewScopeRepo(allowed={("sub-m", "sub-o")})
    with TestClient(app) as client:
        app.state.auth_repo = repo
        app.state.view_scope_repo = scope
        app.state.location_repo = _Sink()
        app.state.ws_reauth_interval_s = _TICK
        yield app, client, repo, scope


def _publish(app: FastAPI, client: TestClient, subject_id: str) -> None:
    broadcaster: LiveBroadcaster = app.state.live_broadcaster
    assert client.portal is not None
    client.portal.call(broadcaster.publish, _fix(subject_id))


def test_live_feed_closes_when_the_member_is_revoked_mid_stream(
    live: tuple[FastAPI, TestClient, FakeAuthRepo, FakeViewScopeRepo],
) -> None:
    app, client, repo, _ = live
    token, pid = _member_token(repo, "sub-m")
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/locations/live") as ws:
        _publish(app, client, "sub-o")
        assert ws.receive_json()["subject_id"] == "sub-o"
        _revoke_principal(repo, pid)
        assert _closed_with(ws) == 4401


def test_an_idle_live_socket_is_rechecked(
    live: tuple[FastAPI, TestClient, FakeAuthRepo, FakeViewScopeRepo],
) -> None:
    # No fix ever arrives: the re-check must not wait on traffic.
    _, client, repo, _ = live
    token, _pid = _member_token(repo, "sub-m")
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/locations/live") as ws:
        asyncio.run(auth_service.logout(repo, token))
        assert _closed_with(ws) == 4401


def test_removal_from_the_family_group_stops_that_subjects_fixes(
    live: tuple[FastAPI, TestClient, FakeAuthRepo, FakeViewScopeRepo],
) -> None:
    app, client, repo, scope = live
    token, _pid = _member_token(repo, "sub-m")
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/locations/live") as ws:
        _publish(app, client, "sub-o")
        assert ws.receive_json()["subject_id"] == "sub-o"
        scope.allowed.clear()
        time.sleep(_TICK * 4)
        _publish(app, client, "sub-o")
        _publish(app, client, "sub-m")
        # The member's own fix arrives; the removed subject's, published first, never does.
        assert ws.receive_json()["subject_id"] == "sub-m"


def test_the_owner_live_feed_stays_open_across_ticks(
    live: tuple[FastAPI, TestClient, FakeAuthRepo, FakeViewScopeRepo],
) -> None:
    app, client, repo, _ = live
    client.cookies.set(_COOKIE, _owner_token(repo))
    with client.websocket_connect("/api/locations/live") as ws:
        time.sleep(_TICK * 4)
        _publish(app, client, "sub-o")
        assert ws.receive_json()["subject_id"] == "sub-o"


# --- jcode terminal ------------------------------------------------------------------


@pytest.fixture
def jcode(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect]]:
    connect = _FakeConnect()
    monkeypatch.setattr(jcode_terminal.websockets, "connect", connect)
    app = _app(jcode_url="http://jcode:9100", jcode_token="t")
    repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        app.state.jcode_client = FakeJcodeClient()
        app.state.ws_reauth_interval_s = _TICK
        yield app, client, repo, connect


def _share_cookie(repo: FakeAuthRepo, sid: str) -> tuple[str, str]:
    key, record = asyncio.run(auth_service.mint_jcode_share(repo, sid, "guest", 24.0))
    redeemed = asyncio.run(auth_service.redeem_jcode_share(repo, key))
    assert redeemed is not None
    return redeemed[0], record.id


def test_a_revoked_shares_terminal_closes(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    _, client, repo, connect = jcode
    token, share_id = _share_cookie(repo, "sess-a")
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        ws.send_bytes(b"ls\n")
        assert ws.receive_bytes() == b"ls\n"  # the share's terminal works while it's live
        assert asyncio.run(repo.revoke_jcode_share(share_id, "sess-a"))
        assert _closed_with(ws) == 4401
    assert connect.upstreams[0].closed  # the upstream PTY socket is dropped too


def test_revoking_a_share_closes_its_terminal_at_once(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    # A tick far longer than the test: only the revoke route's kick can close it.
    app, client, repo, _ = jcode
    app.state.ws_reauth_interval_s = 600.0
    token, share_id = _share_cookie(repo, "sess-a")
    owner = TestClient(app)
    owner.cookies.set(_COOKIE, _owner_token(repo))
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"x"
        assert owner.delete(f"/api/jcode/sessions/sess-a/shares/{share_id}").status_code == 204
        assert _closed_with(ws) == 4401


def test_an_expired_shares_terminal_closes(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    _, client, repo, _ = jcode
    token, share_id = _share_cookie(repo, "sess-a")
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        for p in repo.principals:
            if p.id == share_id:
                p.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        assert _closed_with(ws) == 4401


def test_the_owner_jcode_terminal_stays_open_across_ticks(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    _, client, repo, _ = jcode
    client.cookies.set(_COOKIE, _owner_token(repo))
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        time.sleep(_TICK * 4)
        ws.send_bytes(b"still here")
        assert ws.receive_bytes() == b"still here"


# --- jlaunch terminal ----------------------------------------------------------------


@pytest.fixture
def jlaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FakeAuthRepo]]:
    monkeypatch.setattr(jlaunch_terminal.websockets, "connect", _FakeConnect())
    app = _app(jlaunch_url="http://jlaunch:9101", jlaunch_token="t")
    repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        app.state.ws_reauth_interval_s = _TICK
        yield client, repo


def test_the_jlaunch_terminal_closes_on_logout_and_survives_ticks(
    jlaunch: tuple[TestClient, FakeAuthRepo],
) -> None:
    client, repo = jlaunch
    token = _owner_token(repo)
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/jlaunch/jobs/job1/terminal") as ws:
        time.sleep(_TICK * 4)
        ws.send_text("hi")
        assert ws.receive_text() == "hi"
        asyncio.run(auth_service.logout(repo, token))
        assert _closed_with(ws) == 4401


# --- a re-check that cannot be made ---------------------------------------------------


class _FlakyRepo(FakeAuthRepo):
    """Re-checks (`touch=False`) raise while `fail_rechecks` is positive; the handshake's
    own lookup is never affected."""

    fail_rechecks = 0

    async def find_principal_by_session_token_hash(
        self, token_hash: str, *, device_key: str | None = None, touch: bool = True
    ) -> Any:
        if not touch and self.fail_rechecks > 0:
            self.fail_rechecks -= 1
            raise ConnectionError("pool timeout")
        return await super().find_principal_by_session_token_hash(
            token_hash, device_key=device_key, touch=touch
        )


def _flaky_owner_terminal(app: FastAPI, client: TestClient, fails: int) -> _FlakyRepo:
    repo = _FlakyRepo()
    app.state.auth_repo = repo
    client.cookies.set(_COOKIE, _owner_token(repo))
    repo.fail_rechecks = fails
    return repo


def test_a_transient_check_failure_keeps_the_terminal_open(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    app, client, _, _ = jcode
    repo = _flaky_owner_terminal(app, client, fails=2)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        time.sleep(_TICK * 6)
        assert repo.fail_rechecks == 0  # both failures were spent, and retried past
        ws.send_bytes(b"still here")
        assert ws.receive_bytes() == b"still here"


def test_persistent_check_failures_close_1011_not_4401(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    app, client, _, _ = jcode
    _flaky_owner_terminal(app, client, fails=10**6)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        assert _closed_with(ws) == 1011


def test_a_revoke_after_a_failed_check_still_closes_4401(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    app, client, _, _ = jcode
    repo = _flaky_owner_terminal(app, client, fails=1)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        time.sleep(_TICK * 3)
        _revoke_principal(repo, repo.principals[0].id)
        assert _closed_with(ws) == 4401


def test_a_recheck_does_not_stamp_last_seen(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    _, client, repo, _ = jcode
    client.cookies.set(_COOKIE, _owner_token(repo))
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"x"
        before = repo.touches
        time.sleep(_TICK * 4)
        assert repo.touches == before


def test_a_revoke_by_a_differently_spelled_id_closes_at_once(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    app, client, repo, _ = jcode
    app.state.ws_reauth_interval_s = 600.0
    token, share_id = _share_cookie(repo, "sess-a")
    owner = TestClient(app)
    owner.cookies.set(_COOKIE, _owner_token(repo))
    client.cookies.set(_COOKIE, token)
    with client.websocket_connect("/api/jcode/sessions/sess-a/terminal") as ws:
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"x"
        url = f"/api/jcode/sessions/sess-a/shares/{share_id.upper()}"
        assert owner.delete(url).status_code == 204
        assert _closed_with(ws) == 4401


def test_a_malformed_share_id_is_404(
    jcode: tuple[FastAPI, TestClient, FakeAuthRepo, _FakeConnect],
) -> None:
    app, _, repo, _ = jcode
    owner = TestClient(app)
    owner.cookies.set(_COOKIE, _owner_token(repo))
    assert owner.delete("/api/jcode/sessions/sess-a/shares/not-a-uuid").status_code == 404


# --- the bridge itself ---------------------------------------------------------------


class _Blocking:
    """A browser and an upstream that never produce a frame, recording cancellation."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def _forever(self, who: str) -> Any:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(who)
            raise

    async def receive(self) -> Any:
        return await self._forever("browser")

    def __aiter__(self) -> "_Blocking":
        return self

    async def __anext__(self) -> Any:
        return await self._forever("upstream")


@pytest.mark.parametrize("module", [jcode_terminal, jlaunch_terminal])
def test_cancelling_the_bridge_cancels_both_pumps(module: Any) -> None:
    async def scenario() -> list[str]:
        ends = _Blocking()
        bridge = asyncio.ensure_future(module._bridge(ends, ends))
        await asyncio.sleep(0.01)
        bridge.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bridge
        return ends.cancelled

    assert sorted(asyncio.run(scenario())) == ["browser", "upstream"]
