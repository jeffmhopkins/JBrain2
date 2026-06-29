"""The /api/debug/* surface: capability-token gating plus the prompt-completion,
read-only-SQL guard, logs proxy, and live-routing routes (with the LLM router,
gateway, settings store, and supervisor all faked). The read-only SQL round-trip
against real Postgres lives in test_capability_pg."""

import asyncio
import io
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from jbrain.api.debug import VisionRequest, _jsonable, _run_vision
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.llm.errors import LlmError
from jbrain.llm.types import LlmResult, LlmUsage
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeLocalGateway, FakeSettingsStore

_DB = "postgresql+asyncpg://nobody@localhost:1/none"


class _StubRouter:
    """The three LlmRouter methods the debug complete route touches. Raises for the
    sentinel task 'bad' so the 400 mapping is exercised."""

    async def effective_spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        if task == "bad":
            raise LlmError("unknown LLM task: 'bad'")
        return ("local", "gpt-oss-120b")

    async def effective_reasoning_effort(
        self, task: str, strength: str | None = None
    ) -> str | None:
        return "high"

    async def complete(self, task: str, **kw: Any) -> LlmResult:
        return LlmResult(
            text=f"echo:{kw['user_text']}",
            parsed=None,
            usage=LlmUsage(input_tokens=3, output_tokens=5),
        )


class _FakeResp:
    def __init__(self, status_code: int, text: str, json_body: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._json = json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("unexpected error status")

    def json(self) -> Any:
        return self._json


class _FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # Service names (e.g. "claude-shim") whose /logs return 404 — a not-running peer.
        self.down: set[str] = set()

    async def get(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> _FakeResp:
        self.calls.append((url, params or {}))
        if url.endswith("/nope") or any(url == f"/logs/{s}" for s in self.down):
            return _FakeResp(404, "")
        if url == "/update/status":
            return _FakeResp(
                200,
                "",
                json_body={
                    "state": "running",
                    "exit_code": None,
                    "log_tail": "[update] syncing local models",
                },
            )
        if url == "/metrics":
            return _FakeResp(
                200,
                "",
                json_body={
                    "mem_total_bytes": 130_000_000_000,
                    "mem_available_bytes": 8_000_000_000,
                    "swap_total_bytes": 2_000_000_000,
                    "swap_free_bytes": 1_500_000_000,
                    "disk_total_bytes": 2_000_000_000_000,
                    "disk_free_bytes": 1_200_000_000_000,
                    # gpu/power/load carry the values main's /host/metrics test asserts.
                    "load_1m": 4.2,
                    "load_5m": 0.7,
                    "load_15m": 0.8,
                    "uptime_seconds": 86400,
                    "gpu_busy_percent": 97.0,
                    "fan_rpm": None,
                    "apu_power_w": 88.5,
                    # Deliberately NOT sorted, so the route's sort is what's asserted.
                    "containers": [
                        {"service": "comfyui", "mem_bytes": 2_000_000_000},
                        {"service": "local-llm", "mem_bytes": 100_000_000_000},
                        {"service": "db", "mem_bytes": 300_000_000},
                    ],
                },
            )
        if url == "/processes":
            return _FakeResp(
                200,
                "",
                json_body={
                    # Out of order on purpose; the route sorts by rss_bytes desc.
                    "processes": [
                        {
                            "service": "comfyui",
                            "pid": 201,
                            "rss_bytes": 2_000_000_000,
                            "command": "python /opt/ComfyUI/main.py",
                        },
                        {
                            "service": "local-llm",
                            "pid": 102,
                            "rss_bytes": 38_000_000_000,
                            "command": "llama-server --model /models/qwen3-vl-30b/x.gguf",
                        },
                        {
                            "service": "local-llm",
                            "pid": 101,
                            "rss_bytes": 64_000_000_000,
                            "command": "llama-server --model /models/gpt-oss-120b/x.gguf",
                        },
                    ]
                },
            )
        return _FakeResp(200, "log line one\nlog line two")

    async def aclose(self) -> None:
        pass


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", _DB)
    kw.setdefault("xai_api_key", "test-xai")
    kw.setdefault("anthropic_api_key", "test-anthropic")
    kw.setdefault("debug_access_enabled", True)
    kw.setdefault("supervisor_token", "sek")
    return Settings(**kw)


@pytest.fixture
def debug_client() -> Iterator[tuple[TestClient, str]]:
    app = create_app(_settings())
    repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        app.state.llm_router = _StubRouter()
        app.state.settings_store = FakeSettingsStore()
        app.state.local_gateway = FakeLocalGateway()
        app.state.supervisor_client = _FakeSupervisor()
        key, _ = asyncio.run(auth_service.mint_capability(repo, "claude", ttl_hours=24))
        yield client, key


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _state(client: TestClient) -> Any:
    # The TestClient's `.app` is loosely typed (ASGI callable), so reach app.state
    # — where the fixture wired the fakes — through a cast.
    return cast(Any, client.app).state


# --- gating -----------------------------------------------------------------


def test_surface_absent_when_flag_disabled() -> None:
    app = create_app(_settings(debug_access_enabled=False))
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        # Router not mounted at all → 404, no oracle that the surface exists.
        assert client.get("/api/debug/whoami").status_code == 404


def test_requires_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.get("/api/debug/whoami").status_code == 401
    assert client.get("/api/debug/whoami", headers=_auth("garbage")).status_code == 401


def test_revoked_token_is_rejected(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    repo = _state(client).auth_repo
    principal = asyncio.run(auth_service.authenticate_capability(repo, key))
    assert principal is not None
    asyncio.run(repo.revoke_capability(principal.id))
    assert client.get("/api/debug/whoami", headers=_auth(key)).status_code == 401


def test_whoami_reports_scopes(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    body = client.get("/api/debug/whoami", headers=_auth(key)).json()
    assert body["kind"] == "capability_token" and body["label"] == "claude"
    assert "sql.read" in body["scopes"] and "host.metrics" in body["scopes"]


# --- self-service token lifecycle (console kill switch) ---------------------


def test_suspend_self_then_token_is_rejected(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    # Suspending its own token succeeds, then the SAME key no longer authenticates.
    assert client.post("/api/debug/suspend-self", headers=_auth(key)).status_code == 204
    assert client.get("/api/debug/whoami", headers=_auth(key)).status_code == 401
    repo = _state(client).auth_repo
    assert asyncio.run(repo.list_capabilities())[0].suspended_at is not None


def test_revoke_self_kills_the_token(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    assert client.post("/api/debug/revoke-self", headers=_auth(key)).status_code == 204
    assert client.get("/api/debug/whoami", headers=_auth(key)).status_code == 401
    repo = _state(client).auth_repo
    assert asyncio.run(repo.list_capabilities())[0].revoked_at is not None


def test_self_lifecycle_requires_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.post("/api/debug/suspend-self").status_code == 401
    assert client.post("/api/debug/revoke-self", headers=_auth("garbage")).status_code == 401


# --- live activity feed -----------------------------------------------------


def test_activity_feed_records_commands(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    client.get("/api/debug/whoami", headers=_auth(key))
    client.post("/api/debug/complete", headers=_auth(key), json={"user_text": "hi"})
    body = client.get("/api/debug/activity", headers=_auth(key)).json()
    by_kind = {e["kind"]: e for e in body["events"]}
    assert "whoami" in by_kind and "complete" in by_kind
    # The command detail (the prompt) is captured so the console shows what ran.
    assert by_kind["complete"]["detail"] == "hi"
    assert by_kind["whoami"]["detail"] == ""
    # The poll endpoint never records itself, so the feed doesn't grow on every read.
    assert all(not e["path"].startswith("/api/debug/activity") for e in body["events"])
    assert body["last"] >= 2


def test_activity_feed_requires_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.get("/api/debug/activity").status_code == 401


# --- prompt completion ------------------------------------------------------


def test_complete_returns_output_and_resolved_model(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    resp = client.post(
        "/api/debug/complete",
        headers=_auth(key),
        json={"system": "sys", "user_text": "hello", "strength": "high"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "echo:hello"
    assert body["provider"] == "local" and body["model"] == "gpt-oss-120b"
    assert body["output_tokens"] == 5


def test_complete_maps_router_error_to_400(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    resp = client.post(
        "/api/debug/complete", headers=_auth(key), json={"user_text": "x", "task": "bad"}
    )
    assert resp.status_code == 400


def test_complete_defaults_to_high_tier_when_unspecified(
    debug_client: tuple[TestClient, str],
) -> None:
    client, key = debug_client
    resp = client.post("/api/debug/complete", headers=_auth(key), json={"user_text": "hi"})
    assert resp.status_code == 200 and resp.json()["text"] == "echo:hi"


# --- vision iteration -------------------------------------------------------


class _FakeBlobs:
    """A BlobStore that hands back one canned image regardless of digest."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def get(self, sha256: str) -> bytes:
        return self._data


class _RecordingVisionRouter:
    """Records the kwargs each complete() saw so the test can assert the prompt
    override and image actually reached the adapter."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def effective_spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        return ("local", "qwen3-vl-30b")

    async def complete(self, task: str, **kw: Any) -> LlmResult:
        self.calls.append({"task": task, **kw})
        return LlmResult(
            text=f"caption:{kw['user_text']}",
            parsed=None,
            usage=LlmUsage(input_tokens=7, output_tokens=11),
        )


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (120, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _attachment() -> Any:
    return cast(
        Any,
        SimpleNamespace(
            id=uuid.uuid4(), sha256="deadbeef", media_type="image/png", filename="me.png"
        ),
    )


def test_run_vision_uses_the_shipped_prompt_by_default() -> None:
    from jbrain.ingest.ocr import DESCRIPTION_SYSTEM

    router = _RecordingVisionRouter()
    body = VisionRequest(attachment_id=uuid.uuid4(), task="vision.caption")
    out = asyncio.run(
        _run_vision(cast(Any, router), cast(Any, _FakeBlobs(_png_bytes())), _attachment(), body)
    )
    assert out.provider == "local" and out.model == "qwen3-vl-30b" and out.task == "vision.caption"
    assert out.text.startswith("caption:") and out.filename == "me.png"
    call = router.calls[0]
    # Routed as the vision task, with the shipped caption prompt and one image.
    assert call["task"] == "vision.caption" and call["strength"] == "vision"
    assert call["system"] == DESCRIPTION_SYSTEM and call["images"] and "me.png" in call["user_text"]


def test_run_vision_applies_a_system_override() -> None:
    router = _RecordingVisionRouter()
    body = VisionRequest(
        attachment_id=uuid.uuid4(), task="vision.ocr", system="ONLY transcribe legible text."
    )
    asyncio.run(
        _run_vision(cast(Any, router), cast(Any, _FakeBlobs(_png_bytes())), _attachment(), body)
    )
    assert router.calls[0]["task"] == "vision.ocr"
    assert router.calls[0]["system"] == "ONLY transcribe legible text."


def test_run_vision_rejects_an_unknown_task() -> None:
    body = VisionRequest(attachment_id=uuid.uuid4(), task="vision.bogus")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            _run_vision(
                cast(Any, _RecordingVisionRouter()),
                cast(Any, _FakeBlobs(_png_bytes())),
                _attachment(),
                body,
            )
        )
    assert excinfo.value.status_code == 400


def test_vision_route_requires_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    resp = client.post("/api/debug/vision", json={"attachment_id": str(uuid.uuid4())})
    assert resp.status_code == 401


# --- async completion jobs --------------------------------------------------


def test_complete_async_runs_as_a_job(debug_client: tuple[TestClient, str]) -> None:
    import time

    client, key = debug_client
    sub = client.post("/api/debug/complete-async", headers=_auth(key), json={"user_text": "hi"})
    assert sub.status_code == 202
    job_id = sub.json()["job_id"]

    status: dict[str, Any] = {"status": "pending"}
    for _ in range(60):  # the background task resolves on the app's loop between polls
        status = client.get(f"/api/debug/jobs/{job_id}", headers=_auth(key)).json()
        if status["status"] != "pending":
            break
        time.sleep(0.05)
    assert status["status"] == "done"
    assert status["result"]["text"] == "echo:hi"
    assert status["error"] is None


def test_async_job_error_is_surfaced(debug_client: tuple[TestClient, str]) -> None:
    import time

    client, key = debug_client
    # The stub router raises for task 'bad' -> the job records an error, not a crash.
    job_id = client.post(
        "/api/debug/complete-async", headers=_auth(key), json={"user_text": "x", "task": "bad"}
    ).json()["job_id"]
    status: dict[str, Any] = {"status": "pending"}
    for _ in range(60):
        status = client.get(f"/api/debug/jobs/{job_id}", headers=_auth(key)).json()
        if status["status"] != "pending":
            break
        time.sleep(0.05)
    assert status["status"] == "error" and status["result"] is None and status["error"]


def test_async_routes_require_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    assert client.post("/api/debug/complete-async", json={"user_text": "x"}).status_code == 401
    assert client.get("/api/debug/jobs/whatever").status_code == 401
    # A bearer is valid but the job id is unknown.
    assert client.get("/api/debug/jobs/nope", headers=_auth(key)).status_code == 404


# --- local-model load / unload ----------------------------------------------


def test_load_unload_409_when_hosting_off(debug_client: tuple[TestClient, str]) -> None:
    # Local hosting is off in the fixture settings, so the gateway actions 409 —
    # exercising the debug load/unload routes and their shared guard.
    client, key = debug_client
    assert (
        client.post("/api/debug/llm/local-models/foo/load", headers=_auth(key)).status_code == 409
    )
    assert (
        client.post("/api/debug/llm/local-models/foo/unload", headers=_auth(key)).status_code == 409
    )


# --- read-only SQL guard ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE app.notes SET body = 'x'",
        "DELETE FROM app.notes",
        "SELECT 1; DROP TABLE app.notes",
        "INSERT INTO app.notes VALUES (1)",
    ],
)
def test_sql_rejects_non_read_statements(debug_client: tuple[TestClient, str], sql: str) -> None:
    client, key = debug_client
    resp = client.post("/api/debug/sql", headers=_auth(key), json={"sql": sql})
    assert resp.status_code == 400


def test_jsonable_coerces_db_types() -> None:
    import datetime as dt
    import uuid

    uid = uuid.uuid4()
    assert _jsonable("s") == "s" and _jsonable(3) == 3 and _jsonable(None) is None
    assert _jsonable(uid) == str(uid)
    assert _jsonable(dt.date(2026, 6, 22)) == "2026-06-22"
    assert _jsonable(b"abc") == "<3 bytes>"
    assert _jsonable([1, uid]) == [1, str(uid)]
    assert _jsonable({"k": uid}) == {"k": str(uid)}

    class _Weird:
        def __str__(self) -> str:
            return "weird"

    assert _jsonable(_Weird()) == "weird"


# --- logs -------------------------------------------------------------------


def test_logs_proxies_to_supervisor(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    resp = client.get("/api/debug/logs/api", headers=_auth(key), params={"tail": 50})
    assert resp.status_code == 200 and "log line one" in resp.text
    assert _state(client).supervisor_client.calls == [("/logs/api", {"tail": 50})]


def test_logs_unknown_service_404s(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    assert client.get("/api/debug/logs/nope", headers=_auth(key)).status_code == 404


def test_jcode_logs_aggregates_the_system(debug_client: tuple[TestClient, str]) -> None:
    # One pull returns the whole code-mode system — control server + shim + gateway —
    # labeled, so debugging a turn doesn't need three round-trips.
    client, key = debug_client
    resp = client.get("/api/debug/jcode/logs", headers=_auth(key), params={"tail": 100})
    assert resp.status_code == 200
    for svc in ("jcode", "claude-shim", "local-llm"):
        assert f"===== {svc} =====" in resp.text
    assert "log line one" in resp.text
    calls = [u for u, _ in _state(client).supervisor_client.calls]
    assert calls == ["/logs/jcode", "/logs/claude-shim", "/logs/local-llm"]


def test_jcode_logs_tolerates_a_not_running_service(debug_client: tuple[TestClient, str]) -> None:
    # Mid-bring-up the shim may be down — its section reads "(service not running)" and
    # the pull still succeeds with the others.
    client, key = debug_client
    _state(client).supervisor_client.down = {"claude-shim"}
    resp = client.get("/api/debug/jcode/logs", headers=_auth(key))
    assert resp.status_code == 200
    assert "===== claude-shim =====\n(service not running)" in resp.text
    assert "log line one" in resp.text  # jcode + local-llm still came through


def test_update_status_proxies_to_supervisor(debug_client: tuple[TestClient, str]) -> None:
    # The updater one-shot runs outside the compose project, so /debug/logs can't see
    # it — /debug/update/status proxies the supervisor for the read-only console.
    client, key = debug_client
    resp = client.get("/api/debug/update/status", headers=_auth(key), params={"tail": 120})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "running"
    assert body["log_tail"] == "[update] syncing local models"
    assert ("/update/status", {"tail": 120}) in _state(client).supervisor_client.calls


def test_update_status_requires_the_debug_token(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.get("/api/debug/update/status").status_code == 401


# --- host breakdown (/host) + gateway logs + host telemetry (/host/metrics) --


def test_host_proxies_and_sorts_processes(debug_client: tuple[TestClient, str]) -> None:
    # /debug/host merges /metrics + /processes and returns per-container AND raw
    # per-process RSS biggest-first, so the console can attribute the total.
    client, key = debug_client
    resp = client.get("/api/debug/host", headers=_auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mem_total_bytes"] == 130_000_000_000
    assert body["apu_power_w"] == 88.5
    # Sorted descending by mem_bytes, regardless of the supervisor's order.
    services = [c["service"] for c in body["containers"]]
    assert services == ["local-llm", "comfyui", "db"]
    # Raw per-process RSS, biggest first — the two llama-server PIDs are told apart
    # by their --model path, so the 120B (101) outranks the vision model (102).
    pids = [p["pid"] for p in body["processes"]]
    assert pids == [101, 102, 201]
    assert "gpt-oss-120b" in body["processes"][0]["command"]
    calls = [u for u, _ in _state(client).supervisor_client.calls]
    assert "/metrics" in calls and "/processes" in calls


def test_host_requires_the_debug_token(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.get("/api/debug/host").status_code == 401


def test_whoami_reports_host_scope(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    body = client.get("/api/debug/whoami", headers=_auth(key)).json()
    assert "host.read" in body["scopes"]
    assert "host.metrics" in body["scopes"]


def test_gateway_logs_tails_the_engine_stdout(debug_client: tuple[TestClient, str]) -> None:
    # The gateway's OWN /logs (llama-server slot lifecycle), tailed to the last N lines —
    # the read that shows whether a Stop releases a slot or the engine keeps generating.
    client, key = debug_client
    _state(client).local_gateway.logs_text = "slot launch\nrelease 1\nrelease 2\nrelease 3"
    resp = client.get("/api/debug/llm/gateway-logs", headers=_auth(key), params={"tail": 2})
    assert resp.status_code == 200
    assert resp.text == "release 2\nrelease 3"  # only the last 2 lines


def test_gateway_logs_502_when_gateway_unreachable(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    _state(client).local_gateway.fail_logs = True
    assert client.get("/api/debug/llm/gateway-logs", headers=_auth(key)).status_code == 502


def test_gateway_logs_requires_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.get("/api/debug/llm/gateway-logs").status_code == 401


def test_host_metrics_proxies_supervisor(debug_client: tuple[TestClient, str]) -> None:
    # The one physical read: GPU busy %, APU power, load — proxied from the supervisor so a
    # debug session can watch the device across a Stop.
    client, key = debug_client
    resp = client.get("/api/debug/host/metrics", headers=_auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["gpu_busy_percent"] == 97.0 and body["apu_power_w"] == 88.5
    assert ("/metrics", {}) in _state(client).supervisor_client.calls


def test_host_metrics_requires_a_valid_bearer(debug_client: tuple[TestClient, str]) -> None:
    client, _ = debug_client
    assert client.get("/api/debug/host/metrics").status_code == 401


# --- live routing -----------------------------------------------------------


def test_read_and_switch_routing(debug_client: tuple[TestClient, str]) -> None:
    client, key = debug_client
    assert client.get("/api/debug/llm", headers=_auth(key)).status_code == 200
    resp = client.put(
        "/api/debug/llm",
        headers=_auth(key),
        json={"tasks": {"agent.turn": {"provider": "claude"}}},
    )
    assert resp.status_code == 200
    task = next(t for t in resp.json()["tasks"] if t["id"] == "agent.turn")
    assert task["provider"] == "claude"
