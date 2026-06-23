"""The /api/debug/* surface: capability-token gating plus the prompt-completion,
read-only-SQL guard, logs proxy, and live-routing routes (with the LLM router,
gateway, settings store, and supervisor all faked). The read-only SQL round-trip
against real Postgres lives in test_capability_pg."""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from jbrain.api.debug import _jsonable
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
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("unexpected error status")


class _FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> _FakeResp:
        self.calls.append((url, params or {}))
        if url.endswith("/nope"):
            return _FakeResp(404, "")
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
    assert "sql.read" in body["scopes"]


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
    kinds = [e["kind"] for e in body["events"]]
    assert "whoami" in kinds and "complete" in kinds
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
