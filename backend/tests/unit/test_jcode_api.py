"""The jcode api surface: owner-gating of the routes and the stop/restart proxy
(no DB — the gating decisions land before any query; DB writes are neutralized)."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode
from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.jcode import FakeJcodeClient

OWNER = PrincipalInfo(id="owner1", kind="owner", label="owner")
NON_OWNER = PrincipalInfo(id="cap1", kind="capability_token", label="token")


def _app(principal: PrincipalInfo, *, jcode_client: object) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode.router, prefix="/api")
    app.state.jcode_client = jcode_client
    app.state.session_maker = None  # not reached by the gating tests below
    app.dependency_overrides[current_principal] = lambda: principal
    return app


@pytest.fixture
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the owner-index DB write so a route's happy path runs without a pool."""

    @contextlib.asynccontextmanager
    async def _fake_scoped(*_a: object, **_k: object):
        yield None

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(jcode, "scoped_session", _fake_scoped)
    monkeypatch.setattr(jcode._REPO, "touch", _noop)
    monkeypatch.setattr(jcode._REPO, "delete", _noop)


def test_non_owner_is_forbidden() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.post("/api/jcode/sessions", json={}).status_code == 403
    assert client.get("/api/jcode/sessions").status_code == 403
    # The launcher management routes are owner-only too.
    assert client.patch("/api/jcode/sessions/s1", json={"title": "x"}).status_code == 403
    assert client.post("/api/jcode/sessions/s1/archive").status_code == 403
    assert client.post("/api/jcode/sessions/s1/unarchive").status_code == 403
    assert client.post("/api/jcode/sessions/s1/stop").status_code == 403
    assert client.post("/api/jcode/sessions/s1/restart").status_code == 403


def test_owner_but_unconfigured_is_404() -> None:
    client = TestClient(_app(OWNER, jcode_client=None))
    r = client.post("/api/jcode/sessions", json={"repo": "r"})
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_stop_and_restart_proxy_the_control_server(_no_db: None) -> None:
    import asyncio

    fake = FakeJcodeClient()
    asyncio.run(fake.create_session("r", "main", ""))  # mints sess1 in the fake
    client = TestClient(_app(OWNER, jcode_client=fake))
    # stop/restart flip the control-server status through the proxy (DB write neutralized).
    assert client.post("/api/jcode/sessions/sess1/stop").json()["status"] == "stopped"
    assert client.post("/api/jcode/sessions/sess1/restart").json()["status"] == "ready"


def test_stop_restart_are_owner_gated() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.post("/api/jcode/sessions/sess1/stop").status_code == 403
    assert client.post("/api/jcode/sessions/sess1/restart").status_code == 403


def test_preview_open_status_close() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/sess1/preview").json() == {
        "enabled": True,
        "url": None,
    }
    opened = client.post("/api/jcode/sessions/sess1/preview", json={}).json()
    assert opened["url"].endswith(".trycloudflare.com")
    assert client.delete("/api/jcode/sessions/sess1/preview").status_code == 204


def test_preview_reports_disabled() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient(preview_enabled=False)))
    assert client.get("/api/jcode/sessions/sess1/preview").json()["enabled"] is False


def test_preview_is_owner_gated() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/sess1/preview").status_code == 403
    assert client.post("/api/jcode/sessions/sess1/preview", json={}).status_code == 403


def test_malformed_sid_is_404_before_any_db_or_control_call() -> None:
    # A sid carrying a path char never reaches the DB (None here) or the control
    # server — _valid_sid 404s first (review S2). session_maker is None, so reaching
    # it would error; a clean 404 proves the guard runs before the body.
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/bad.id").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/reset").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/stop").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/restart").status_code == 404


# --- Master power switch --------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")


class _FakeSupervisor:
    """A minimal stand-in for app.state.supervisor_client: records start/stop calls and
    reflects them into the /status snapshot, so a toggle round-trips through the fake."""

    def __init__(self, states: dict[str, str], *, start_404: tuple[str, ...] = ()) -> None:
        self.states = dict(states)
        self.calls: list[tuple[str, str]] = []
        self._start_404 = set(start_404)

    async def get(self, path: str, headers: object = None) -> _FakeResp:
        assert path == "/status"
        return _FakeResp(
            200,
            {"containers": [{"service": s, "state": st} for s, st in self.states.items()]},
        )

    async def post(self, path: str, json: dict, headers: object = None) -> _FakeResp:
        action, service = path.lstrip("/"), json["service"]
        self.calls.append((action, service))
        if action == "start" and service in self._start_404:
            return _FakeResp(404)
        self.states[service] = "running" if action == "start" else "exited"
        return _FakeResp(202)


class _FakeStore:
    async def jcode_model(self, _ctx: object) -> str:
        return ""  # falls back to settings.jcode_model


def _power_app(
    principal: PrincipalInfo,
    supervisor: _FakeSupervisor,
    *,
    jcode_client: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode.router, prefix="/api")
    app.state.jcode_client = jcode_client if jcode_client is not None else FakeJcodeClient()
    app.state.settings = SimpleNamespace(
        local_llm_enabled=False,  # skip the gateway probe; power is about services here
        supervisor_token="tok",
        jcode_model="qwen3-coder-next",
    )
    app.state.settings_store = _FakeStore()
    app.state.supervisor_client = supervisor
    app.state.local_gateway = None
    app.dependency_overrides[current_principal] = lambda: principal
    return app


def test_power_status_reports_on_when_all_services_running() -> None:
    sup = _FakeSupervisor({"local-llm": "running", "claude-shim": "running", "jcode": "running"})
    client = TestClient(_power_app(OWNER, sup))
    body = client.get("/api/jcode/power").json()
    assert body["on"] is True
    assert body["provisioned"] is True
    assert {s["name"] for s in body["services"]} == {"local-llm", "claude-shim", "jcode"}


def test_power_status_off_when_a_service_is_down() -> None:
    sup = _FakeSupervisor({"local-llm": "running", "claude-shim": "exited", "jcode": "running"})
    body = TestClient(_power_app(OWNER, sup)).get("/api/jcode/power").json()
    assert body["on"] is False
    assert body["provisioned"] is True


def test_power_status_unprovisioned_when_services_absent() -> None:
    sup = _FakeSupervisor({"api": "running"})  # jcode services never created
    body = TestClient(_power_app(OWNER, sup)).get("/api/jcode/power").json()
    assert body["on"] is False
    assert body["provisioned"] is False


def test_power_status_counts_live_sessions() -> None:
    import asyncio

    fake = FakeJcodeClient()
    asyncio.run(fake.create_session("r", "main", ""))  # a ready (live) session
    sup = _FakeSupervisor({"local-llm": "running", "claude-shim": "running", "jcode": "running"})
    body = TestClient(_power_app(OWNER, sup, jcode_client=fake)).get("/api/jcode/power").json()
    assert body["live_sessions"] == 1


def test_power_on_starts_services_in_order() -> None:
    sup = _FakeSupervisor({"local-llm": "exited", "claude-shim": "exited", "jcode": "exited"})
    client = TestClient(_power_app(OWNER, sup))
    body = client.post("/api/jcode/power", json={"on": True}).json()
    # Gateway first, then shim, then the control server.
    assert sup.calls == [("start", "local-llm"), ("start", "claude-shim"), ("start", "jcode")]
    assert body["on"] is True


def test_power_off_stops_services_in_reverse_order() -> None:
    sup = _FakeSupervisor({"local-llm": "running", "claude-shim": "running", "jcode": "running"})
    client = TestClient(_power_app(OWNER, sup))
    body = client.post("/api/jcode/power", json={"on": False}).json()
    assert sup.calls == [("stop", "jcode"), ("stop", "claude-shim"), ("stop", "local-llm")]
    assert body["on"] is False


def test_power_on_skips_unprovisioned_service() -> None:
    # A box without the jcode profile (only the coder gateway): starting the missing
    # services 404s and is skipped, not fatal.
    sup = _FakeSupervisor({"local-llm": "exited"}, start_404=("claude-shim", "jcode"))
    client = TestClient(_power_app(OWNER, sup))
    assert client.post("/api/jcode/power", json={"on": True}).status_code == 200
    assert ("start", "local-llm") in sup.calls


def test_power_is_owner_gated() -> None:
    sup = _FakeSupervisor({"local-llm": "running"})
    client = TestClient(_power_app(NON_OWNER, sup))
    assert client.get("/api/jcode/power").status_code == 403
    assert client.post("/api/jcode/power", json={"on": True}).status_code == 403
