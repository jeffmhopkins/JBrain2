"""The owner tunnel's phones (docs/plans/OWNER_TUNNEL_PLAN.md §3.4, wave A3): the guard
that refuses any owner-site request not from an enrolled, unrevoked phone, the session a
phone mints, which then works only from that phone, and the owner's list, rename and
revoke."""

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jbrain.api import client_ip
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.main import create_app
from jbrain.owner_tunnel.devices import OwnerDeviceInfo
from jbrain.owner_tunnel.guard import OwnerDeviceGuard, kick_device
from tests.unit.fakes import FakeAuthRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_COOKIE = "jbrain_session"
PHONE = "a" * 64
OTHER = "b" * 64
PROXY = "172.18.0.3"
_OWNER_SITE = {"x-jbrain-ingress": "owner-tunnel"}


def _site(key: str | None) -> dict[str, str]:
    return {**_OWNER_SITE, "x-jbrain-device": key or ""}


@dataclasses.dataclass
class FakeOwnerDeviceRepo:
    devices: list[OwnerDeviceInfo] = dataclasses.field(default_factory=list)
    touched: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    ctxs: list[SessionContext] = dataclasses.field(default_factory=list)
    # Mirrors the real revoke's cascade onto the bound sessions.
    auth: FakeAuthRepo | None = None

    def add(self, key_hash: str, label: str = "Pixel", *, revoked: bool = False) -> str:
        device_id = f"dev-{len(self.devices)}"
        self.devices.append(
            OwnerDeviceInfo(
                id=device_id,
                label=label,
                key_hash=key_hash,
                enrolled_at=datetime.now(UTC),
                last_seen_at=None,
                last_ingress=None,
                revoked_at=datetime.now(UTC) if revoked else None,
            )
        )
        if self.auth is not None and not revoked:
            self.auth.device_keys[device_id] = key_hash
        return device_id

    async def touch_active(self, key_hash: str, ingress: str) -> OwnerDeviceInfo | None:
        self.touched.append((key_hash, ingress))
        for d in self.devices:
            if d.key_hash == key_hash and d.revoked_at is None:
                return d
        return None

    async def create(self, label: str, key_hash: str, public_key: bytes) -> OwnerDeviceInfo:
        raise NotImplementedError

    async def list_all(self, ctx: SessionContext) -> list[OwnerDeviceInfo]:
        self.ctxs.append(ctx)
        return list(self.devices)

    async def rename(self, ctx: SessionContext, device_id: str, label: str) -> bool:
        self.ctxs.append(ctx)
        for i, d in enumerate(self.devices):
            if d.id == device_id:
                self.devices[i] = dataclasses.replace(d, label=label)
                return True
        return False

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool:
        self.ctxs.append(ctx)
        for i, d in enumerate(self.devices):
            if d.id == device_id and d.revoked_at is None:
                self.devices[i] = dataclasses.replace(d, revoked_at=datetime.now(UTC))
                if self.auth is not None:
                    self.auth.device_keys.pop(device_id, None)
                return True
        return False


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def phones(repo: FakeAuthRepo) -> FakeOwnerDeviceRepo:
    return FakeOwnerDeviceRepo(auth=repo)


@pytest.fixture
def client(repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo) -> Iterator[TestClient]:
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    # The test client connects from the pinned proxy address, so its headers are Caddy's.
    with client_ip.pinned(proxy=(PROXY,)), TestClient(app, client=(PROXY, 50000)) as c:
        app.state.auth_repo = repo
        app.state.owner_device_repo = phones
        yield c


def _owner_token(repo: FakeAuthRepo) -> str:
    async def go() -> str:
        key = await auth_service.rotate_owner_key(repo)
        return await auth_service.login(repo, key, "t")

    return asyncio.run(go())


def _phone_session(client: TestClient, key: str) -> str:
    resp = client.post("/api/owner-devices/session", headers=_site(key))
    assert resp.status_code == 204, resp.text
    token = resp.cookies.get(_COOKIE)
    assert token
    client.cookies.clear()
    return token


def _me(client: TestClient, token: str, headers: dict[str, str] | None = None) -> int:
    resp = client.get("/api/auth/me", headers={"cookie": f"{_COOKIE}={token}", **(headers or {})})
    return resp.status_code


# --- the guard ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", [None, OTHER, "A" * 64, "a" * 63], ids=["none", "unknown", "upper", "short"]
)
def test_owner_site_refuses_a_request_without_an_enrolled_phone(
    client: TestClient, phones: FakeOwnerDeviceRepo, key: str | None
) -> None:
    phones.add(PHONE)
    resp = client.get("/api/healthz", headers=_site(key))
    assert resp.status_code == 403
    assert resp.json() == {"detail": "device not enrolled"}


def test_owner_site_refuses_a_revoked_phone(
    client: TestClient, phones: FakeOwnerDeviceRepo
) -> None:
    phones.add(PHONE, revoked=True)
    assert client.get("/api/healthz", headers=_site(PHONE)).status_code == 403


def test_owner_site_lets_an_enrolled_phone_through_and_stamps_it(
    client: TestClient, phones: FakeOwnerDeviceRepo
) -> None:
    phones.add(PHONE)
    assert client.get("/api/healthz", headers=_site(PHONE)).status_code == 200
    assert phones.touched == [(PHONE, "owner-tunnel")]


@pytest.mark.parametrize("ingress", ["public", "lan", "preview", None])
def test_other_ingresses_never_consult_the_phones(
    client: TestClient, phones: FakeOwnerDeviceRepo, ingress: str | None
) -> None:
    headers = {"x-jbrain-device": PHONE}
    if ingress is not None:
        headers["x-jbrain-ingress"] = ingress
    assert client.get("/api/healthz", headers=headers).status_code == 200
    assert phones.touched == []


def test_ingress_header_from_a_non_proxy_peer_is_ignored(
    repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    # A peer that is not Caddy (jcode, cloudflared straight at the api) cannot claim the
    # owner site: the guard does not engage, and a device key it sends is not believed.
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    with client_ip.pinned(proxy=(PROXY,)), TestClient(app, client=("172.18.0.9", 50000)) as c:
        app.state.auth_repo = repo
        app.state.owner_device_repo = phones
        assert c.get("/api/healthz", headers=_site(None)).status_code == 200
    assert phones.touched == []


def test_owner_site_refuses_a_websocket_without_an_enrolled_phone(
    client: TestClient, phones: FakeOwnerDeviceRepo
) -> None:
    phones.add(PHONE)
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/locations/live", headers=_site(OTHER)),
    ):
        pass
    assert closed.value.code == 4403


def test_guard_leaves_non_api_paths_alone(client: TestClient, phones: FakeOwnerDeviceRepo) -> None:
    client.get("/", headers=_site(None))
    assert phones.touched == []


# --- the phone's session -----------------------------------------------------------------


def test_a_phone_session_works_only_from_that_phone(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    _owner_token(repo)
    phones.add(PHONE)
    phones.add(OTHER, "Tablet")
    token = _phone_session(client, PHONE)
    assert repo.sessions[-1].device_id == "dev-0"
    assert repo.sessions[-1].label == "phone: Pixel"
    assert _me(client, token, _site(PHONE)) == 200
    # The other enrolled phone passes the guard but cannot use this phone's session.
    assert _me(client, token, _site(OTHER)) == 401
    # Off the owner site there is no device key, so a bound session is not accepted at all.
    assert _me(client, token) == 401
    assert _me(client, token, {"x-jbrain-ingress": "public", "x-jbrain-device": PHONE}) == 401


def test_an_unbound_session_does_not_work_over_the_owner_site(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    # A cookie phished from the public site must not ride an enrolled phone's tunnel.
    token = _owner_token(repo)
    phones.add(PHONE)
    assert _me(client, token) == 200
    assert _me(client, token, _site(PHONE)) == 401


def test_minting_needs_the_owner_site(client: TestClient, repo: FakeAuthRepo) -> None:
    _owner_token(repo)
    resp = client.post("/api/owner-devices/session", headers={"x-jbrain-device": PHONE})
    assert resp.status_code == 403
    assert len(repo.sessions) == 1


def test_minting_without_an_owner_is_a_conflict(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    phones.add(PHONE)
    resp = client.post("/api/owner-devices/session", headers=_site(PHONE))
    assert resp.status_code == 409
    assert repo.sessions == []


def test_the_phone_cookie_lives_as_long_as_the_idle_window(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    _owner_token(repo)
    phones.add(PHONE)
    resp = client.post("/api/owner-devices/session", headers=_site(PHONE))
    cookie = resp.headers["set-cookie"].lower()
    assert f"max-age={int(auth_service.BOUND_SESSION_IDLE.total_seconds())}" in cookie
    assert "samesite=lax" in cookie
    assert "httponly" in cookie


# --- the owner's list ------------------------------------------------------------------


def _as_owner(client: TestClient, repo: FakeAuthRepo) -> None:
    client.cookies.set(_COOKIE, _owner_token(repo))


def test_owner_lists_phones(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    _as_owner(client, repo)
    phones.add(PHONE)
    phones.add(OTHER, "Old", revoked=True)
    body: list[dict[str, Any]] = client.get("/api/owner-devices").json()
    assert [(d["label"], d["revoked"]) for d in body] == [("Pixel", False), ("Old", True)]
    assert "key_hash" not in body[0]
    assert phones.ctxs[-1].principal_kind == "owner"


def test_owner_renames_a_phone(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    _as_owner(client, repo)
    did = phones.add(PHONE)
    assert client.patch(f"/api/owner-devices/{did}", json={"label": "  Mine "}).status_code == 204
    assert phones.devices[0].label == "Mine"
    assert client.patch(f"/api/owner-devices/{did}", json={"label": "   "}).status_code == 422
    assert client.patch(f"/api/owner-devices/{did}", json={"label": "x" * 81}).status_code == 422
    assert client.patch("/api/owner-devices/nope", json={"label": "a"}).status_code == 404


def test_revoking_a_phone_ends_its_session(
    client: TestClient, repo: FakeAuthRepo, phones: FakeOwnerDeviceRepo
) -> None:
    owner = _owner_token(repo)
    did = phones.add(PHONE)
    phone_token = _phone_session(client, PHONE)
    assert _me(client, phone_token, _site(PHONE)) == 200
    client.cookies.set(_COOKIE, owner)
    assert client.post(f"/api/owner-devices/{did}/revoke").status_code == 204
    assert client.post(f"/api/owner-devices/{did}/revoke").status_code == 404
    client.cookies.clear()
    # The guard now refuses the phone outright, before its session is even looked at.
    assert _me(client, phone_token, _site(PHONE)) == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/owner-devices"),
        ("PATCH", "/api/owner-devices/dev-0"),
        ("POST", "/api/owner-devices/dev-0/revoke"),
    ],
)
def test_phone_management_is_owner_only(client: TestClient, method: str, path: str) -> None:
    resp = client.request(method, path, json={"label": "a"} if method == "PATCH" else None)
    assert resp.status_code == 401


# --- the check is held for the life of a request ------------------------------------------
# Driven as raw ASGI: the test client buffers a whole response, so it cannot hold one open.


class _Sent:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.arrived = asyncio.Event()

    async def __call__(self, message: Any) -> None:
        self.messages.append(dict(message))
        self.arrived.set()

    def bodies(self) -> int:
        return sum(
            1 for m in self.messages if m["type"] in ("http.response.body", "websocket.send")
        )


async def _never_ending(scope: Any, receive: Any, send: Any) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        while True:
            await send({"type": "websocket.send", "text": "tick"})
            await asyncio.sleep(0.01)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    while True:
        await send({"type": "http.response.body", "body": b"data: tick\n\n", "more_body": True})
        await asyncio.sleep(0.01)


def _held(
    phones: FakeOwnerDeviceRepo,
    interval: float,
    inner: Any = _never_ending,
    kind: str = "http",
) -> tuple[OwnerDeviceGuard, dict[str, Any], _Sent]:
    state = SimpleNamespace(owner_device_repo=phones, ws_reauth_interval_s=interval)
    scope = {
        "type": kind,
        "path": "/api/stream",
        "headers": [(k.encode(), v.encode()) for k, v in _site(PHONE).items()],
        "client": (PROXY, 1),
        "app": SimpleNamespace(state=state),
    }
    return OwnerDeviceGuard(inner), scope, _Sent()


async def _receive() -> dict[str, Any]:
    await asyncio.Event().wait()
    raise AssertionError


async def _until(sent: _Sent, bodies: int) -> None:
    while sent.bodies() < bodies:
        sent.arrived.clear()
        await sent.arrived.wait()


def _run(guard: OwnerDeviceGuard, scope: dict[str, Any], sent: _Sent) -> asyncio.Future[None]:
    return asyncio.ensure_future(guard(scope, _receive, sent))


def test_a_stream_ends_when_its_phone_is_revoked_on_the_next_tick() -> None:
    async def go() -> None:
        phones = FakeOwnerDeviceRepo()
        did = phones.add(PHONE)
        guard, scope, sent = _held(phones, interval=0.05)
        with client_ip.pinned(proxy=(PROXY,)):
            task = _run(guard, scope, sent)
            await _until(sent, 3)
            await phones.revoke(SessionContext(), did)
            await asyncio.wait_for(task, 2)
        assert sent.messages[0]["status"] == 200

    asyncio.run(go())


def test_revoke_ends_a_stream_at_once_through_kick() -> None:
    async def go() -> None:
        phones = FakeOwnerDeviceRepo()
        did = phones.add(PHONE)
        # A tick far longer than the test: only the kick can end the stream.
        guard, scope, sent = _held(phones, interval=3600)
        with client_ip.pinned(proxy=(PROXY,)):
            task = _run(guard, scope, sent)
            await _until(sent, 3)
            await phones.revoke(SessionContext(), did)
            kick_device(scope["app"], did)
            await asyncio.wait_for(task, 2)
        assert scope["app"].state.owner_device_watchers == {}

    asyncio.run(go())


def test_a_kick_for_an_enrolled_phone_changes_nothing() -> None:
    async def go() -> None:
        phones = FakeOwnerDeviceRepo()
        did = phones.add(PHONE)
        guard, scope, sent = _held(phones, interval=3600)
        with client_ip.pinned(proxy=(PROXY,)):
            task = _run(guard, scope, sent)
            await _until(sent, 3)
            kick_device(scope["app"], did)
            await _until(sent, sent.bodies() + 10)
            assert not task.done()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A request cancelled from outside takes its inner app with it and leaves no watcher.
        assert scope["app"].state.owner_device_watchers == {}

    asyncio.run(go())


def test_a_socket_closes_when_its_phone_is_revoked() -> None:
    async def go() -> None:
        phones = FakeOwnerDeviceRepo()
        did = phones.add(PHONE)
        guard, scope, sent = _held(phones, interval=0.05, kind="websocket")
        with client_ip.pinned(proxy=(PROXY,)):
            task = _run(guard, scope, sent)
            await _until(sent, 3)
            await phones.revoke(SessionContext(), did)
            await asyncio.wait_for(task, 2)
        assert sent.messages[-1] == {"type": "websocket.close", "code": 4403, "reason": ""}

    asyncio.run(go())


class _FlakyPhones(FakeOwnerDeviceRepo):
    failing: bool = False
    failures: int = 0
    failed = asyncio.Event()

    async def touch_active(self, key_hash: str, ingress: str) -> OwnerDeviceInfo | None:
        if self.failing:
            self.failures += 1
            self.failed.set()
            raise OSError("db blip")
        return await super().touch_active(key_hash, ingress)


def test_a_db_blip_does_not_end_a_stream_but_a_lasting_failure_does() -> None:
    async def go() -> None:
        phones = _FlakyPhones()
        phones.add(PHONE)
        guard, scope, sent = _held(phones, interval=0.02)
        with client_ip.pinned(proxy=(PROXY,)):
            task = _run(guard, scope, sent)
            await _until(sent, 2)
            phones.failing = True
            while phones.failures < 2:
                phones.failed.clear()
                await phones.failed.wait()
            phones.failing = False
            await asyncio.sleep(0.1)
            assert not task.done()
            phones.failing = True
            await asyncio.wait_for(task, 2)

    asyncio.run(go())


def test_revoke_before_the_response_starts_is_a_403() -> None:
    async def slow(scope: Any, receive: Any, send: Any) -> None:
        await asyncio.sleep(5)

    async def go() -> None:
        phones = FakeOwnerDeviceRepo()
        did = phones.add(PHONE)
        guard, scope, sent = _held(phones, interval=0.05, inner=slow)
        with client_ip.pinned(proxy=(PROXY,)):
            task = _run(guard, scope, sent)
            await asyncio.sleep(0.02)
            await phones.revoke(SessionContext(), did)
            await asyncio.wait_for(task, 2)
        assert sent.messages[0]["status"] == 403

    asyncio.run(go())


def test_an_inner_error_still_propagates() -> None:
    async def broken(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    async def go() -> None:
        phones = FakeOwnerDeviceRepo()
        phones.add(PHONE)
        guard, scope, sent = _held(phones, interval=3600, inner=broken)
        with client_ip.pinned(proxy=(PROXY,)), pytest.raises(RuntimeError):
            await guard(scope, _receive, sent)

    asyncio.run(go())
