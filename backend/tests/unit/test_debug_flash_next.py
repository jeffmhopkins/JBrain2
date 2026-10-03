"""The Flash-Next debug tooling (docs/plans/FLASH_NEXT_ENGINE_PLAN.md §5): the debug engine
switch (since F3a a thin wrapper over the owner switch's ONE orchestration — drain, one-engine
stop/start, smoke, rollback) and its one-engine guarantee, the engine-aware log reads, the
slot save/restore probe, and the allowlisted perplexity one-shot. The supervisor, the gateway
and llama-server's upstream are all faked; nothing here touches docker or a model."""

import asyncio
import dataclasses
import math
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain.api import debug
from jbrain.api import engine as engine_api
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.llm import local_catalog
from jbrain.llm.engine_switch import EngineSwitcher, Timings
from jbrain.llm.local_gateway import LocalGatewayError
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeLocalGateway, FakeSettingsStore

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_UP = {"running", "restarting", "paused", "removing"}
_ENGINE_SERVICES = ("local-llm", "flash-next")
_FAST = Timings(
    drain_s=0.0, settle_s=0.0, memory_settle_s=0.0, poll_s=0.0, gate_wait_s=0.0, engine_cache_s=0.0
)
_TERMINAL = {"done", "rolled_back", "failed"}


class _Resp:
    def __init__(self, status_code: int, json_body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://supervisor")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(self.status_code)
            )

    def json(self) -> Any:
        return self._json


class _Supervisor:
    """A stateful supervisor: container states that /stop and /start really change, and a
    shared event log so a test can assert ORDER across the gateway and the supervisor. It
    also records any start that happened while another engine was still up — the §4d
    violation the switch exists to make impossible."""

    def __init__(self, events: list[str], states: dict[str, str]) -> None:
        self.events = events
        self.states = states
        self.violations: list[str] = []
        # Services whose /start 404s even though /status lists them (a race the route
        # must still roll back from).
        self.start_404: set[str] = set()
        # Services whose /stop is accepted but never takes effect.
        self.stuck: set[str] = set()
        self.perplexity_state = "none"
        self.perplexity_tail = ""
        self.perplexity_posts: list[dict] = []
        self.perplexity_answer = 202
        # Another one-shot kind in flight (update, refresh, ...), or None.
        self.oneshot: str | None = None
        # An older supervisor with no /oneshot route.
        self.no_oneshot_route = False

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> _Resp:
        assert headers == {"Authorization": "Bearer sek"}
        if url == "/status":
            containers = [{"service": s, "state": st} for s, st in self.states.items()]
            return _Resp(200, {"containers": containers})
        if url == "/oneshot":
            if self.no_oneshot_route:
                return _Resp(404)
            running = "perplexity" if self.perplexity_state == "running" else self.oneshot
            return _Resp(200, {"running": running})
        if url == "/perplexity/status":
            body = {"state": self.perplexity_state, "exit_code": None}
            return _Resp(200, {**body, "log_tail": self.perplexity_tail})
        if url.startswith("/logs/"):
            service = url.removeprefix("/logs/")
            self.events.append(f"logs {service}")
            return _Resp(200, text=f"{service} log")
        return _Resp(404)

    async def post(self, url: str, json: dict | None = None, headers: dict | None = None) -> _Resp:
        assert headers == {"Authorization": "Bearer sek"}
        body = json or {}
        if url in ("/start", "/stop"):
            service = body["service"]
            self.events.append(f"{url[1:]} {service}")
            if service not in self.states:
                return _Resp(404)
            if url == "/start":
                if service in self.start_404:
                    return _Resp(404)
                others = [s for s in _ENGINE_SERVICES if s != service]
                if service in _ENGINE_SERVICES and any(self.states.get(o) in _UP for o in others):
                    self.violations.append(service)
                self.states[service] = "running"
            elif service not in self.stuck:
                self.states[service] = "exited"
            return _Resp(202, {"service": service})
        if url == "/perplexity":
            self.perplexity_posts.append(body)
            self.events.append("perplexity")
            if self.perplexity_answer == 202:
                self.perplexity_state = "running"
                return _Resp(202, {"oneshot": "jbrain-perplexity-1"})
            return _Resp(self.perplexity_answer)
        return _Resp(404)

    async def aclose(self) -> None:
        pass


class _Gateway(FakeLocalGateway):
    def __init__(self, events: list[str], running: set[str]) -> None:
        super().__init__(running)
        self.events = events
        self.n_slots = 4
        self.fail_slots = False

    async def unload(self, served_model: str) -> None:
        self.events.append(f"unload {served_model}")
        await super().unload(served_model)

    async def slots(self, served_model: str) -> list[dict[str, object]]:
        if self.fail_slots:
            raise LocalGatewayError("simulated")
        return [{"id": i} for i in range(self.n_slots)]


@pytest.fixture(autouse=True)
def _quiet_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """No nightly window and no workflow run unless a test says otherwise."""

    async def _quiet(maker: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(engine_api, "quiet_window_guard", _quiet)


@pytest.fixture
def box(tmp_path: Path) -> Iterator[tuple[TestClient, str, Any]]:
    settings = Settings(
        secure_cookies=False,
        database_url=_DB,
        xai_api_key="test-xai",
        anthropic_api_key="test-anthropic",
        debug_access_enabled=True,
        supervisor_token="sek",
        local_models_dir=str(tmp_path),
        local_models=["gpt-oss-120b", "qwen3.8-flash-next"],
    )
    app = create_app(settings)
    repo = FakeAuthRepo()
    events: list[str] = []
    with TestClient(app) as client:
        app.state.auth_repo = repo
        app.state.settings_store = FakeSettingsStore()
        app.state.local_gateway = _Gateway(events, {"gpt-oss-120b"})
        app.state.supervisor_client = _Supervisor(
            events, {"api": "running", "local-llm": "running", "flash-next": "exited"}
        )
        app.state.gpu_probe = None
        app.state.engine_switcher = EngineSwitcher(_FAST)
        key, _ = asyncio.run(auth_service.mint_capability(repo, "claude", ttl_hours=24))
        yield client, key, app.state


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _switch(
    client: TestClient, key: str, engine: str, **extra: Any
) -> tuple[httpx.Response, dict[str, Any] | None]:
    """POST the debug switch; when it was accepted, poll until it ends and return the final
    switch status (None for a refusal, which touched nothing)."""
    resp = client.post(
        "/api/debug/llm/engine", json={"engine": engine, **extra}, headers=_auth(key)
    )
    if resp.status_code != 202:
        return resp, None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
        status = body.get("switch")
        if status and status["id"] == resp.json()["id"] and status["stage"] in _TERMINAL:
            return resp, status
        time.sleep(0.005)
    raise AssertionError("the switch never ended")


# --- auth on every new route ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/debug/llm/engine", None),
        ("POST", "/api/debug/llm/engine", {"engine": "flash-next"}),
        ("POST", "/api/debug/llm/slot-probe", {"synth_tokens": 64}),
        ("POST", "/api/debug/llm/perplexity", {}),
        ("GET", "/api/debug/llm/perplexity/status", None),
    ],
)
def test_every_new_route_requires_the_debug_token(
    box: tuple[TestClient, str, Any], method: str, path: str, body: Any
) -> None:
    client, _, state = box
    for headers in ({}, _auth("garbage")):
        resp = client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 401
    # Nothing reached the supervisor or the gateway on the way to the refusal.
    assert state.supervisor_client.events == []
    assert state.supervisor_client.perplexity_posts == []


# --- engine read + switch ---------------------------------------------------------------


def test_engine_read_reports_both_services(box: tuple[TestClient, str, Any]) -> None:
    client, key, _ = box
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["desired"] == "standard" and body["effective"] == "standard"
    assert body["services"]["standard"] == {"service": "local-llm", "state": "running"}
    assert body["services"]["flash-next"] == {"service": "flash-next", "state": "exited"}
    assert body["running"] == ["standard"] and body["consistent"] is True
    assert body["perplexity_running"] is False


def test_engine_read_marks_an_unprovisioned_service_missing(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    del state.supervisor_client.states["flash-next"]
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["services"]["flash-next"]["state"] == "missing"


def test_switch_orders_drain_unload_stop_wait_start_smoke_persist(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    sup = state.supervisor_client

    resp, final = _switch(client, key, "flash-next")

    assert resp.status_code == 202, resp.text
    assert resp.json()["source"] == "debug" and resp.json()["target"] == "flash-next"
    assert final is not None and final["stage"] == "done", final
    assert [s["stage"] for s in final["stages"]] == [
        "draining",
        "stopping",
        "starting",
        "loading",
        "smoke",
        "done",
    ]
    assert sup.events == ["unload gpt-oss-120b", "stop local-llm", "start flash-next"]
    assert sup.violations == []
    assert state.local_gateway.loaded == ["qwen3.8-flash-next"]
    assert [s["probe"] for s in final["smoke"]] == ["text", "tool", "image"]
    assert state.settings_store.values["llm_local_engine"] == "flash-next"
    assert state.settings_store.values["llm_local_engine_effective"] == "flash-next"
    # Admission was closed for the switch and is open again.
    assert state.settings_store.values["llm_local_admission"] == {"closed": False}
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["desired"] == "flash-next" and body["effective"] == "flash-next"
    assert body["running"] == ["flash-next"] and body["consistent"] is True
    assert body["switching"] is False and body["admission"]["closed"] is False


def test_switch_back_to_standard_is_symmetric(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.states.update({"local-llm": "exited", "flash-next": "running"})
    state.settings_store.values["llm_local_engine"] = "flash-next"
    state.settings_store.values["llm_local_engine_effective"] = "flash-next"
    state.local_gateway = _Gateway(sup.events, {"qwen3.8-flash-next"})

    _, final = _switch(client, key, "standard")

    assert final is not None and final["stage"] == "done"
    assert sup.events == ["unload qwen3.8-flash-next", "stop flash-next", "start local-llm"]
    # Standard has no sole model: the smoke runs on its smallest installed tool-capable one,
    # and gpt-oss has no projector, so no image probe.
    assert final["model"] == "gpt-oss-120b"
    assert [s["probe"] for s in final["smoke"]] == ["text", "tool"]
    assert state.settings_store.values["llm_local_engine"] == "standard"
    assert state.settings_store.values["llm_local_engine_effective"] == "standard"


def test_switch_stops_both_others_when_the_box_is_already_inconsistent(
    box: tuple[TestClient, str, Any],
) -> None:
    """Both up is the freeze state; a switch must end it, not add to it."""
    client, key, state = box
    sup = state.supervisor_client
    sup.states.update({"local-llm": "running", "flash-next": "running"})

    _, final = _switch(client, key, "standard")

    assert final is not None and final["stage"] == "done"
    assert "stop flash-next" in sup.events
    assert "start local-llm" not in sup.events  # already the target and up
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["running"] == ["standard"]


def test_switch_to_the_engine_already_up_is_a_noop(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    resp = client.post("/api/debug/llm/engine", json={"engine": "standard"}, headers=_auth(key))
    assert resp.status_code == 202 and resp.json()["stage"] == "done"
    assert state.supervisor_client.events == []
    assert state.local_gateway.unloaded == []
    # Never closed admission: nothing was drained.
    assert "llm_local_admission" not in state.settings_store.values


def test_switch_to_an_unprovisioned_engine_touches_nothing(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    del sup.states["flash-next"]

    resp, _ = _switch(client, key, "flash-next")

    assert resp.status_code == 409
    assert "not provisioned" in resp.json()["detail"]
    assert sup.events == []
    assert sup.states["local-llm"] == "running"
    assert "llm_local_engine" not in state.settings_store.values
    assert "llm_local_admission" not in state.settings_store.values


def test_switch_rolls_back_when_start_404s(box: tuple[TestClient, str, Any]) -> None:
    """The container vanished between /status and /start: the previous engine goes back
    up, the setting is untouched, and the status says so."""
    client, key, state = box
    sup = state.supervisor_client
    sup.start_404.add("flash-next")

    _, final = _switch(client, key, "flash-next")

    assert final is not None and final["stage"] == "rolled_back"
    assert "standard was put back" in final["reason"]
    assert sup.events[-1] == "start local-llm"
    assert sup.states["local-llm"] == "running"
    assert sup.violations == []
    assert "llm_local_engine" not in state.settings_store.values
    # The desire is untouched; what serves is recorded as the engine put back.
    assert state.settings_store.values["llm_local_engine_effective"] == "standard"
    assert state.settings_store.values["llm_local_admission"] == {"closed": False}


def test_switch_never_starts_the_target_while_the_other_is_still_up(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.stuck.add("local-llm")

    _, final = _switch(client, key, "flash-next")

    assert final is not None and final["stage"] == "failed"
    assert "NOT started" in final["reason"]
    assert "start flash-next" not in sup.events
    assert sup.violations == []
    assert "llm_local_engine" not in state.settings_store.values


def test_switch_rolls_back_when_the_target_never_reports_running(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    real_post = sup.post

    async def start_but_crash(url: str, json: dict | None = None, headers: dict | None = None):
        resp = await real_post(url, json=json, headers=headers)
        if url == "/start" and (json or {}).get("service") == "flash-next":
            sup.states["flash-next"] = "exited"  # crashed on launch
        return resp

    monkeypatch.setattr(sup, "post", start_but_crash)
    _, final = _switch(client, key, "flash-next")

    assert final is not None and final["stage"] == "rolled_back"
    assert "did not report running" in final["reason"]
    assert sup.events[-2:] == ["stop flash-next", "start local-llm"]
    assert "llm_local_engine" not in state.settings_store.values


def test_switch_refuses_while_a_perplexity_run_owns_the_box(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    state.supervisor_client.perplexity_state = "running"

    resp, _ = _switch(client, key, "flash-next")

    assert resp.status_code == 409
    assert state.supervisor_client.events == []


def test_switch_aborts_before_stopping_anything_if_unload_fails(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    state.local_gateway.fail_unload = True

    _, final = _switch(client, key, "flash-next")

    assert final is not None and final["stage"] == "failed"
    assert "no engine was stopped" in final["reason"]
    assert "already unloaded" in final["reason"]
    assert not any(e.startswith(("stop", "start")) for e in state.supervisor_client.events)
    assert state.settings_store.values["llm_local_admission"] == {"closed": False}


def test_switch_rejects_an_unknown_engine(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    for body in ({"engine": "turbo"}, {"engine": "standard", "service": "api"}):
        resp = client.post("/api/debug/llm/engine", json=body, headers=_auth(key))
        assert resp.status_code == 422
    assert state.supervisor_client.events == []


def test_engine_read_502s_on_an_unreachable_supervisor(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box

    class _Down(_Supervisor):
        async def get(self, url: str, params: dict | None = None, headers: dict | None = None):
            raise httpx.ConnectError("no route")

    state.supervisor_client = _Down([], {})
    assert client.get("/api/debug/llm/engine", headers=_auth(key)).status_code == 502


# --- engine-aware logs ------------------------------------------------------------------


def test_jcode_logs_follow_the_active_engine(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    state.settings_store.values["llm_local_engine_effective"] = "flash-next"

    resp = client.get("/api/debug/jcode/logs", headers=_auth(key))

    assert resp.status_code == 200
    assert "===== flash-next =====" in resp.text and "local-llm" not in resp.text
    assert state.supervisor_client.events == ["logs jcode", "logs flash-next"]


def test_gateway_and_upstream_logs_name_the_engine(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    state.settings_store.values["llm_local_engine_effective"] = "flash-next"
    state.local_gateway.logs_text = "a\nb"

    resp = client.get("/api/debug/llm/gateway-logs", headers=_auth(key))
    assert resp.status_code == 200 and resp.headers["X-JBrain-Engine"] == "flash-next"

    state.local_gateway.fail_logs = True
    resp = client.get("/api/debug/llm/gateway-logs", headers=_auth(key))
    assert resp.status_code == 502
    assert "/debug/logs/flash-next" in resp.json()["detail"]


def test_upstream_logs_502_names_the_engine(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box

    async def broken(stream: str = "upstream") -> str:
        raise LocalGatewayError("refused")

    monkeypatch.setattr(state.local_gateway, "tail_upstream_logs", broken, raising=False)
    resp = client.get("/api/debug/llm/upstream-logs", headers=_auth(key))
    assert resp.status_code == 502
    assert "active engine: standard" in resp.json()["detail"]


def test_upstream_logs_carry_the_engine_header(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box

    async def tail(stream: str = "upstream") -> str:
        return "x\ny\nz"

    monkeypatch.setattr(state.local_gateway, "tail_upstream_logs", tail, raising=False)
    resp = client.get("/api/debug/llm/upstream-logs", params={"tail": 2}, headers=_auth(key))
    assert resp.text == "y\nz" and resp.headers["X-JBrain-Engine"] == "standard"


def test_an_unreadable_engine_setting_reads_as_the_default(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box

    async def broken(ctx: object) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(state.settings_store, "llm_local_engine_effective", broken)
    resp = client.get("/api/debug/jcode/logs", headers=_auth(key))
    assert resp.status_code == 200
    assert state.supervisor_client.events == ["logs jcode", "logs local-llm"]


# --- slot save/restore probe ------------------------------------------------------------


class _Upstream:
    """llama-server behind llama-swap's /upstream passthrough: slot erase/save/restore and
    a /completion that answers per-slot logprobs. `save_path=False` is a server started
    without --slot-save-path, which refuses every slot action with 501."""

    def __init__(
        self, *, save_path: bool = True, drift: float = 0.0, reprefill: bool = False
    ) -> None:
        self.reprefill = reprefill
        self.save_path = save_path
        self.drift = drift
        self.calls: list[str] = []
        self.restored: set[int] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        path = request.url.path
        assert path.startswith("/upstream/gpt-oss-120b/"), path
        tail = path.removeprefix("/upstream/gpt-oss-120b")
        action = request.url.params.get("action")
        self.calls.append(f"{tail}?{action}" if action else tail)
        if tail.startswith("/slots/"):
            if not self.save_path:
                return httpx.Response(
                    501,
                    json={
                        "error": {
                            "message": "This server does not support slots action. "
                            "Start it with `--slot-save-path`"
                        }
                    },
                )
            slot = int(tail.rsplit("/", 1)[1])
            if action == "save":
                return httpx.Response(
                    200,
                    json={"n_saved": 777, "n_written": 123456, "timings": {"save_ms": 12.5}},
                )
            if action == "restore":
                self.restored.add(slot)
                return httpx.Response(
                    200,
                    json={"n_restored": 777, "n_read": 123456, "timings": {"restore_ms": 3.0}},
                )
            return httpx.Response(200, json={"id_slot": slot, "n_erased": 0})
        assert tail == "/completion"
        body = _json.loads(request.content)
        assert body["n_predict"] == 1 and body["temperature"] == 0
        slot = body["id_slot"]
        shift = self.drift if slot in self.restored else 0.0
        top = [
            {"id": 11, "token": "A", "logprob": -0.1 + shift},
            {"id": 22, "token": "B", "logprob": -2.5},
            {"id": 33, "token": "C", "logprob": -4.0},
        ][: body["n_probs"]]
        return httpx.Response(
            200,
            json={
                "completion_probabilities": [{"id": 11, "logprob": -0.1, "top_logprobs": top}],
                "timings": {"prompt_n": 1 if slot in self.restored and not self.reprefill else 777},
                "tokens_cached": 777,
            },
        )


def _probe(client: TestClient, key: str, **body: Any) -> httpx.Response:
    return client.post(
        "/api/debug/llm/slot-probe", json=body or {"synth_tokens": 120}, headers=_auth(key)
    )


def test_slot_probe_happy_path(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, _ = box
    upstream = _Upstream(drift=0.003)
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))

    resp = _probe(client, key, synth_tokens=120, n_probs=3)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "gpt-oss-120b" and body["engine"] == "standard"
    # Defaults to the LAST two slots, so slot 0 (the persona on Flash-Next) is left alone.
    assert (body["slot_a"], body["slot_b"]) == (2, 3)
    assert upstream.calls == [
        "/slots/2?erase",
        "/slots/3?erase",
        "/completion",
        "/slots/2?save",
        "/slots/3?restore",
        "/completion",
        "/completion",
    ]
    assert body["n_saved"] == 777 and body["n_restored"] == 777
    assert body["file_bytes"] == 123456
    assert body["save_ms"] == 12.5 and body["restore_ms"] == 3.0
    assert [c["id"] for c in body["restored"]["top"]] == [11, 22, 33]
    assert body["restored"]["timings"]["prompt_n"] == 1
    diff = body["restored_vs_warm"]
    assert diff["shared"] == 3 and diff["top1_equal"] is True
    assert math.isclose(diff["max_abs_diff"], 0.003, abs_tol=1e-9)


def test_slot_probe_409_without_a_slot_save_path(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, _ = box
    upstream = _Upstream(save_path=False)
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))

    resp = _probe(client, key, prompt="hello there")

    assert resp.status_code == 409
    assert "--slot-save-path" in resp.json()["detail"]
    # Refused at the first slot action — nothing was primed.
    assert "/completion" not in upstream.calls


def test_slot_probe_never_loads_a_model(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    upstream = _Upstream()
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))
    state.local_gateway = _Gateway([], set())

    assert _probe(client, key).status_code == 409
    assert _probe(client, key, prompt="x", model="other").status_code == 409
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({}, 400),
        ({"prompt": "x", "synth_tokens": 64}, 400),
        ({"prompt": "x", "slot_a": 1, "slot_b": 1}, 400),
        ({"prompt": "x", "slot_a": 0, "slot_b": 9}, 400),
        ({"prompt": "x", "n_probs": 0}, 422),
        ({"prompt": "x", "extra": 1}, 422),
    ],
)
def test_slot_probe_validates_its_input(
    box: tuple[TestClient, str, Any],
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
    status: int,
) -> None:
    client, key, _ = box
    upstream = _Upstream()
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))
    resp = client.post("/api/debug/llm/slot-probe", json=body, headers=_auth(key))
    assert resp.status_code == status
    assert upstream.calls == []


def test_slot_probe_needs_two_slots(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(_Upstream()))
    state.local_gateway.n_slots = 1
    assert _probe(client, key).status_code == 409
    state.local_gateway.fail_slots = True
    assert _probe(client, key).status_code == 502


def test_slot_probe_asks_which_model_when_several_are_resident(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    upstream = _Upstream()
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))
    state.local_gateway = _Gateway([], {"gpt-oss-120b", "qwen3-vl"})
    assert _probe(client, key).status_code == 400
    assert _probe(client, key, prompt="x", model="gpt-oss-120b").status_code == 200


def test_slot_probe_502s_on_an_upstream_failure(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, _ = box

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/completion"):
            return httpx.Response(500, text="kaput")
        return httpx.Response(200, json={})

    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(broken))
    assert _probe(client, key).status_code == 502

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(unreachable))
    assert _probe(client, key).status_code == 502


def test_top_logprobs_reads_the_older_probs_shape() -> None:
    old = {"completion_probabilities": [{"probs": [{"tok_str": "A", "prob": 0.5}]}]}
    assert debug._top_logprobs(old) == [{"id": None, "token": "A", "logprob": math.log(0.5)}]
    assert debug._top_logprobs({}) == []


def test_diff_with_nothing_shared_has_no_number() -> None:
    a = debug.SlotProbeRead(
        slot=0, top=[{"id": 1, "logprob": -1.0}], timings={}, tokens_cached=None
    )
    b = debug.SlotProbeRead(
        slot=1, top=[{"id": 2, "logprob": -1.0}], timings={}, tokens_cached=None
    )
    diff = debug._diff(a, b)
    assert diff.max_abs_diff is None and diff.shared == 0 and diff.top1_equal is False


# --- perplexity one-shot ----------------------------------------------------------------


@pytest.fixture
def flash_next_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """A Flash-Next catalog entry (T1 lands the real one) with its shards on disk under
    the box's models dir — the path the route must resolve without hardcoding a name."""
    entry = dataclasses.replace(
        local_catalog.CATALOG[0],
        id="qwen3.8-flash-next",
        engine="flash-next",
        gguf_include="*UD-IQ4_XS*.gguf",
    )
    monkeypatch.setattr(local_catalog, "CATALOG", (*local_catalog.CATALOG, entry))
    quant = tmp_path / "qwen3.8-flash-next" / "UD-IQ4_XS"
    quant.mkdir(parents=True)
    for i in (1, 2):
        (quant / f"Qwen3.8-Flash-Next-UD-IQ4_XS-0000{i}-of-00002.gguf").write_bytes(b"")
    return "/models/qwen3.8-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00002.gguf"


def test_perplexity_resolves_the_catalog_path_and_unloads_first(
    box: tuple[TestClient, str, Any], flash_next_catalog: str
) -> None:
    client, key, state = box
    sup = state.supervisor_client

    resp = client.post("/api/debug/llm/perplexity", json={"chunks": 50}, headers=_auth(key))

    assert resp.status_code == 202, resp.text
    assert sup.perplexity_posts == [{"model_path": flash_next_catalog, "chunks": 50}]
    assert sup.events == ["unload gpt-oss-120b", "perplexity"]
    assert resp.json()["model_path"] == flash_next_catalog


@pytest.mark.parametrize(
    "body",
    [
        {"chunks": 0},
        {"chunks": 201},
        {"chunks": 10, "args": ["-ngl", "0"]},
        {"model_path": "/etc/passwd"},
        {"extra_args": "--override-kv x"},
    ],
)
def test_perplexity_takes_no_free_form_input(
    box: tuple[TestClient, str, Any], flash_next_catalog: str, body: dict
) -> None:
    client, key, state = box
    resp = client.post("/api/debug/llm/perplexity", json=body, headers=_auth(key))
    assert resp.status_code == 422
    assert state.supervisor_client.perplexity_posts == []
    assert state.local_gateway.unloaded == []


def test_perplexity_without_a_catalog_entry_or_weights_is_409(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    monkeypatch.setattr(
        local_catalog,
        "CATALOG",
        tuple(m for m in local_catalog.CATALOG if m.engine == "standard"),
    )
    resp = client.post("/api/debug/llm/perplexity", json={}, headers=_auth(key))
    assert resp.status_code == 409 and "no Flash-Next model" in resp.json()["detail"]

    entry = dataclasses.replace(local_catalog.CATALOG[0], id="fn", engine="flash-next")
    monkeypatch.setattr(local_catalog, "CATALOG", (entry,))
    resp = client.post("/api/debug/llm/perplexity", json={}, headers=_auth(key))
    assert resp.status_code == 409 and "install them in the PWA" in resp.json()["detail"]
    assert state.supervisor_client.perplexity_posts == []


@pytest.mark.parametrize(
    ("answer", "words"), [(404, "not provisioned"), (409, "one-shot"), (400, "refused")]
)
def test_perplexity_maps_supervisor_refusals(
    box: tuple[TestClient, str, Any], flash_next_catalog: str, answer: int, words: str
) -> None:
    client, key, state = box
    state.supervisor_client.perplexity_answer = answer
    resp = client.post("/api/debug/llm/perplexity", json={}, headers=_auth(key))
    assert resp.status_code == 409 and words in resp.json()["detail"]


def test_perplexity_refuses_a_second_run(
    box: tuple[TestClient, str, Any], flash_next_catalog: str
) -> None:
    client, key, state = box
    state.supervisor_client.perplexity_state = "running"
    resp = client.post("/api/debug/llm/perplexity", json={}, headers=_auth(key))
    assert resp.status_code == 409
    assert state.local_gateway.unloaded == []


def test_perplexity_status_parses_the_final_estimate(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.perplexity_state = "exited"
    body = client.get("/api/debug/llm/perplexity/status", headers=_auth(key)).json()
    assert body["ppl"] is None

    sup.perplexity_tail = "[1]5.1,[2]5.3\nFinal estimate: PPL = 5.8123 +/- 0.04117\n"
    body = client.get("/api/debug/llm/perplexity/status", headers=_auth(key)).json()
    assert body["ppl"] == 5.8123 and body["ppl_error"] == 0.04117
    assert body["state"] == "exited"


# --- edges ------------------------------------------------------------------------------


def test_a_supervisor_without_the_perplexity_job_reads_as_not_running(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    real_get = sup.get

    async def older(url: str, params: dict | None = None, headers: dict | None = None):
        if url == "/perplexity/status":
            return _Resp(404)
        return await real_get(url, params=params, headers=headers)

    monkeypatch.setattr(sup, "get", older)
    sup.no_oneshot_route = True
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["perplexity_running"] is False


def test_perplexity_running_comes_from_the_one_oneshot_read(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.perplexity_state = "running"
    real_get = sup.get
    reads: list[str] = []

    async def counting(url: str, params: dict | None = None, headers: dict | None = None):
        reads.append(url)
        return await real_get(url, params=params, headers=headers)

    monkeypatch.setattr(sup, "get", counting)
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["perplexity_running"] is True and body["oneshot"] == "perplexity"
    assert "/perplexity/status" not in reads


def test_a_supervisor_error_on_stop_fails_the_switch_and_nothing_starts(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    sup = state.supervisor_client

    real_post = sup.post

    async def refuse(url: str, json: dict | None = None, headers: dict | None = None):
        if url in ("/stop", "/start"):
            sup.events.append(f"{url} refused")
            return _Resp(500)
        return await real_post(url, json=json, headers=headers)

    monkeypatch.setattr(sup, "post", refuse)
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "failed"
    # Re-issued once, never confirmed — and the engine that is still up is recorded.
    assert sup.events.count("/stop refused") == 2
    assert "did not stop" in final["reason"] and "standard is serving" in final["reason"]
    assert "/start refused" not in sup.events
    assert state.settings_store.values["llm_local_engine_effective"] == "standard"


def test_the_switch_waits_for_a_slow_stop(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop the daemon reports a poll late is waited for, not mistaken for a failure."""
    client, key, state = box
    state.engine_switcher = EngineSwitcher(dataclasses.replace(_FAST, settle_s=30.0))
    sup = state.supervisor_client
    sup.stuck.add("local-llm")
    real_get = sup.get
    polls = {"n": 0}

    async def settle(url: str, params: dict | None = None, headers: dict | None = None):
        if url == "/status" and "stop local-llm" in sup.events:
            polls["n"] += 1
            if polls["n"] == 2:
                sup.states["local-llm"] = "exited"
        return await real_get(url, params=params, headers=headers)

    monkeypatch.setattr(sup, "get", settle)
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "done"
    assert sup.violations == []


def test_without_local_hosting_the_switch_skips_unload_and_smoke_and_the_probe_refuses(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    state.local_gateway = None
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "done"
    assert final["smoke"] == [] and "smoke test skipped" in " ".join(final["notes"])
    assert _probe(client, key).status_code == 409


def test_slot_probe_502s_on_a_slot_action_error(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, _ = box

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="disk full")

    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(broken))
    resp = _probe(client, key)
    assert resp.status_code == 502 and "disk full" in resp.json()["detail"]


# --- review fixes ------------------------------------------------------------------------


def test_switch_refuses_while_any_oneshot_runs(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    for kind in ("update", "refresh", "provision"):
        state.supervisor_client.oneshot = kind
        resp, _ = _switch(client, key, "flash-next")
        assert resp.status_code == 409 and kind in resp.json()["detail"]
    assert state.supervisor_client.events == []
    assert state.local_gateway.unloaded == []


def test_switch_against_an_older_supervisor_still_sees_a_perplexity_run(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.no_oneshot_route = True
    sup.perplexity_state = "running"
    resp, _ = _switch(client, key, "flash-next")
    assert resp.status_code == 409
    sup.perplexity_state = "exited"
    resp, final = _switch(client, key, "flash-next")
    assert resp.status_code == 202 and final is not None and final["stage"] == "done"


def test_a_concurrent_switch_is_refused_not_interleaved(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    switcher: EngineSwitcher = state.engine_switcher

    async def hold() -> None:
        await switcher._lock.acquire()

    asyncio.run(hold())
    try:
        resp, _ = _switch(client, key, "flash-next")
    finally:
        switcher._lock.release()
    assert resp.status_code == 409 and "in progress" in resp.json()["detail"]
    assert state.supervisor_client.events == []


def test_a_crash_looping_engine_is_stopped_before_the_other_starts(
    box: tuple[TestClient, str, Any],
) -> None:
    """A restarting engine re-allocates on every loop: it counts as UP (the predicate the
    supervisor and deploy scripts share), so the switch STOPS it and only then starts the
    target — it neither blocks the switch nor is skipped as down."""
    client, key, state = box
    sup = state.supervisor_client
    sup.states.update({"local-llm": "exited", "flash-next": "restarting"})
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["running"] == ["flash-next"]
    _, final = _switch(client, key, "standard")
    assert final is not None and final["stage"] == "done"
    assert sup.events.index("stop flash-next") < sup.events.index("start local-llm")
    assert sup.violations == []


def test_engine_read_reports_a_fallback(box: tuple[TestClient, str, Any]) -> None:
    """Flash-Next wanted, the deploy fell back to standard: the read says both, and is
    consistent against what actually serves."""
    client, key, state = box
    state.settings_store.values["llm_local_engine"] = "flash-next"
    state.settings_store.values["llm_local_engine_effective"] = "standard"
    body = client.get("/api/debug/llm/engine", headers=_auth(key)).json()
    assert body["desired"] == "flash-next" and body["effective"] == "standard"
    assert body["running"] == ["standard"] and body["consistent"] is True


def test_logs_follow_the_effective_engine_not_the_desired_one(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    state.settings_store.values["llm_local_engine"] = "flash-next"
    resp = client.get("/api/debug/jcode/logs", headers=_auth(key))
    assert resp.status_code == 200
    assert state.supervisor_client.events == ["logs jcode", "logs local-llm"]


def test_rollback_restores_nothing_when_the_target_will_not_stop(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target up (crash-looping counts) but its load fails, and its stop then fails:
    putting the previous engine back would make two. One down is recoverable; two up is a
    freeze. What IS up is recorded as effective."""
    client, key, state = box
    sup = state.supervisor_client
    state.local_gateway.fail_load = True
    real_post = sup.post

    async def crash_then_refuse_stop(
        url: str, json: dict | None = None, headers: dict | None = None
    ):
        service = (json or {}).get("service")
        if url == "/stop" and service == "flash-next":
            sup.events.append("stop flash-next refused")
            return _Resp(500)
        resp = await real_post(url, json=json, headers=headers)
        if url == "/start" and service == "flash-next":
            sup.states["flash-next"] = "restarting"  # up, holding memory, unstoppable
        return resp

    monkeypatch.setattr(sup, "post", crash_then_refuse_stop)
    _, final = _switch(client, key, "flash-next")

    assert final is not None and final["stage"] == "failed"
    assert "nothing was restored" in final["reason"]
    assert "flash-next is serving" in final["reason"]
    assert state.settings_store.values["llm_local_engine_effective"] == "flash-next"
    assert "start local-llm" not in sup.events
    assert sup.violations == []


def test_rollback_restores_nothing_when_the_target_stop_is_not_confirmed(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop is ACCEPTED (202) but /status never shows the target down."""
    client, key, state = box
    sup = state.supervisor_client
    real_post = sup.post

    async def flaky(url: str, json: dict | None = None, headers: dict | None = None):
        service = (json or {}).get("service")
        resp = await real_post(url, json=json, headers=headers)
        if service == "flash-next":
            sup.states["flash-next"] = "created" if url == "/start" else "running"
        return resp

    monkeypatch.setattr(sup, "post", flaky)
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "failed"
    assert "nothing was restored" in final["reason"]
    assert "start local-llm" not in sup.events


def test_rollback_when_the_start_itself_errors(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    real_post = sup.post

    async def start_500(url: str, json: dict | None = None, headers: dict | None = None):
        if url == "/start" and (json or {}).get("service") == "flash-next":
            sup.events.append("start flash-next 500")
            return _Resp(500)
        return await real_post(url, json=json, headers=headers)

    monkeypatch.setattr(sup, "post", start_500)
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "rolled_back"
    assert "standard was put back" in final["reason"]
    assert sup.events[-1] == "start local-llm"
    assert sup.violations == []


def test_rollback_reports_a_restore_that_failed(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.start_404.add("flash-next")
    real_post = sup.post

    async def restore_fails(url: str, json: dict | None = None, headers: dict | None = None):
        if url == "/start" and (json or {}).get("service") == "local-llm":
            return _Resp(500)
        return await real_post(url, json=json, headers=headers)

    monkeypatch.setattr(sup, "post", restore_fails)
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "failed"
    assert "could NOT be put back" in final["reason"]


def test_rollback_with_nothing_previously_up(box: tuple[TestClient, str, Any]) -> None:
    client, key, state = box
    sup = state.supervisor_client
    sup.states["local-llm"] = "exited"
    sup.start_404.add("flash-next")
    _, final = _switch(client, key, "flash-next")
    assert final is not None and final["stage"] == "rolled_back"
    assert "none was put back" in final["reason"]


def test_slot_probe_never_defaults_to_slot_zero(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, state = box
    upstream = _Upstream()
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))
    state.local_gateway.n_slots = 2
    resp = _probe(client, key)
    assert resp.status_code == 409 and "slot 0" in resp.json()["detail"]
    assert upstream.calls == []
    # Named explicitly, slot 0 is the operator's deliberate choice.
    resp = _probe(client, key, prompt="x", slot_a=0, slot_b=1)
    assert resp.status_code == 200


def test_slot_probe_flags_a_restore_the_server_threw_away(
    box: tuple[TestClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, key, _ = box
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(_Upstream()))
    assert _probe(client, key).json()["restore_effective"] is True
    upstream = _Upstream(reprefill=True)
    monkeypatch.setattr(debug, "_UPSTREAM_TRANSPORT", httpx.MockTransport(upstream))
    assert _probe(client, key).json()["restore_effective"] is False


def test_restore_effective_is_unknown_without_timings() -> None:
    bare = debug.SlotProbeRead(slot=0, top=[], timings={}, tokens_cached=None)
    assert debug._restore_effective(bare, bare) is None


def test_perplexity_checks_for_a_oneshot_before_unloading(
    box: tuple[TestClient, str, Any], flash_next_catalog: str
) -> None:
    client, key, state = box
    state.supervisor_client.oneshot = "update"
    resp = client.post("/api/debug/llm/perplexity", json={}, headers=_auth(key))
    assert resp.status_code == 409 and "update" in resp.json()["detail"]
    assert state.local_gateway.unloaded == []
    assert state.supervisor_client.perplexity_posts == []


def test_debug_engine_affecting_routes_refuse_while_a_switch_runs(
    box: tuple[TestClient, str, Any],
) -> None:
    client, key, state = box
    switcher: EngineSwitcher = state.engine_switcher
    asyncio.run(switcher._lock.acquire())
    try:
        for path, body in (
            ("/api/debug/update", None),
            ("/api/debug/backup", None),
            ("/api/debug/refresh?service=api", None),
            ("/api/debug/llm/perplexity", {}),
        ):
            resp = client.post(path, json=body, headers=_auth(key))
            assert resp.status_code == 409, (path, resp.text)
            assert "engine switch is in progress" in resp.json()["detail"]
    finally:
        switcher._lock.release()
    assert state.supervisor_client.perplexity_posts == []


def test_debug_cancel_is_409_outside_draining(box: tuple[TestClient, str, Any]) -> None:
    client, key, _ = box
    resp = client.post("/api/debug/llm/engine/cancel", headers=_auth(key))
    assert resp.status_code == 409 and "draining" in resp.json()["detail"]
    assert client.post("/api/debug/llm/engine/cancel").status_code == 401
