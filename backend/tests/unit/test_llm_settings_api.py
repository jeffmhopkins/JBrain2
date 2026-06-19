"""The /api/settings/llm surface — runtime per-task LLM routing + reasoning
effort — with the store faked; the SQL store's round-trip is covered in
test_settings_pg."""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.llm.router import TASK_DEFAULTS
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeLocalGateway, FakeSettingsStore


def _cloud_settings(**kw: Any) -> Settings:
    """Settings with both cloud API keys present — the normal operating state.
    provider_choices hides a keyless cloud provider, so tests that expect grok or
    Claude to be offered must supply the keys (override with ``xai_api_key=""`` to
    test the hidden case)."""
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    kw.setdefault("xai_api_key", "test-xai")
    kw.setdefault("anthropic_api_key", "test-anthropic")
    return Settings(**kw)


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeSettingsStore]]:
    app = create_app(_cloud_settings())
    auth_repo = FakeAuthRepo()
    store = FakeSettingsStore()
    with TestClient(app) as test_client:
        app.state.auth_repo = auth_repo
        app.state.settings_store = store
        app.state.local_gateway = FakeLocalGateway()
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield test_client, store


def test_requires_auth() -> None:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/llm").status_code == 401


def test_get_defaults_grok_and_low_for_empty_store(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, _ = client
    body = c.get("/api/settings/llm").json()
    assert body["reasoning_efforts"] == ["none", "low", "medium", "high"]
    assert body["reasoning_default"] == "low"
    # No memory meter when hosting is off.
    assert body["host_memory"] is None
    # Local hosting is off by default — only the two cloud providers are offered.
    assert {p["id"] for p in body["providers"]} == {"grok", "claude"}
    grok = next(p for p in body["providers"] if p["id"] == "grok")
    assert grok["supports_reasoning"] is True
    # Every routed task lists with the grok default + the default effort.
    assert {t["id"] for t in body["tasks"]} == set(TASK_DEFAULTS)
    for task in body["tasks"]:
        assert task["provider"] == "grok"
        assert task["reasoning_effort"] == "low"


def test_put_round_trips_effective_values(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    resp = c.put(
        "/api/settings/llm",
        json={
            "tasks": {
                "agent.turn": {"provider": "grok", "reasoning_effort": "high"},
                "note.extract": {"provider": "claude", "reasoning_effort": "high"},
            }
        },
    )
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in resp.json()["tasks"]}
    # grok keeps the stored effort; claude is non-reasoning so effort is null.
    assert tasks["agent.turn"]["provider"] == "grok"
    assert tasks["agent.turn"]["reasoning_effort"] == "high"
    assert tasks["note.extract"]["provider"] == "claude"
    assert tasks["note.extract"]["reasoning_effort"] is None
    # Stored shape: claude drops reasoning_effort entirely.
    stored = cast(dict[str, object], store.values["llm_task_overrides"])
    assert stored["agent.turn"] == {"spec": "xai:grok-4.3", "reasoning_effort": "high"}
    assert stored["note.extract"] == {"spec": "anthropic:claude-sonnet-4-6"}
    # GET reflects the same effective values.
    got = {t["id"]: t for t in c.get("/api/settings/llm").json()["tasks"]}
    assert got["agent.turn"]["reasoning_effort"] == "high"
    assert got["note.extract"]["provider"] == "claude"


def test_put_rejects_unknown_task_provider_and_effort(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    bad_bodies = [
        {"tasks": {"nope.task": {"provider": "grok", "reasoning_effort": "low"}}},
        {"tasks": {"agent.turn": {"provider": "gpt", "reasoning_effort": "low"}}},
        {"tasks": {"agent.turn": {"provider": "grok", "reasoning_effort": "extreme"}}},
        {"tasks": {"agent.turn": {"provider": "grok", "reasoning_effort": "low", "x": 1}}},
    ]
    for body in bad_bodies:
        assert c.put("/api/settings/llm", json=body).status_code == 422
    assert "llm_task_overrides" not in store.values  # nothing leaked


def _authed_client(
    settings: Settings, gateway: FakeLocalGateway | None = None
) -> tuple[TestClient, FakeSettingsStore]:
    """A logged-in client over the given settings (the fixture pins defaults)."""
    app = create_app(settings)
    store = FakeSettingsStore()
    c = TestClient(app)
    c.__enter__()
    app.state.auth_repo = FakeAuthRepo()
    app.state.settings_store = store
    app.state.local_gateway = gateway or FakeLocalGateway()
    key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
    assert (
        c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"}).status_code == 204
    )
    return c, store


def test_local_models_offered_only_when_hosting_enabled() -> None:
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b", "gpt-oss-120b"],
    )
    c, _ = _authed_client(settings)
    providers = {p["id"]: p for p in c.get("/api/settings/llm").json()["providers"]}
    assert set(providers) == {"grok", "claude", "qwen3-vl-30b", "gpt-oss-120b"}
    # The vision model carries its capability; the text reasoner does not.
    assert providers["qwen3-vl-30b"]["supports_vision"] is True
    assert providers["gpt-oss-120b"]["supports_vision"] is False
    assert providers["qwen3-vl-30b"]["supports_reasoning"] is False


def test_cloud_provider_hidden_without_its_api_key() -> None:
    # No XAI key → grok is not offered; the Anthropic key is set → claude still is.
    c, _ = _authed_client(_cloud_settings(xai_api_key=""))
    assert {p["id"] for p in c.get("/api/settings/llm").json()["providers"]} == {"claude"}
    # And the reverse.
    c2, _ = _authed_client(_cloud_settings(anthropic_api_key=""))
    assert {p["id"] for p in c2.get("/api/settings/llm").json()["providers"]} == {"grok"}
    # Neither key, no local hosting → an empty provider list (the screen surfaces
    # any stored override as unavailable rather than crashing).
    c3, _ = _authed_client(_cloud_settings(xai_api_key="", anthropic_api_key=""))
    assert c3.get("/api/settings/llm").json()["providers"] == []


def test_put_rejects_a_keyless_cloud_provider() -> None:
    # grok has no key → not a valid choice → 422, nothing stored.
    c, store = _authed_client(_cloud_settings(xai_api_key=""))
    assert (
        c.put("/api/settings/llm", json={"tasks": {"agent.turn": {"provider": "grok"}}}).status_code
        == 422
    )
    assert "llm_task_overrides" not in store.values


def test_put_routes_a_task_to_an_enabled_local_model() -> None:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b"],
    )
    c, store = _authed_client(settings)
    resp = c.put(
        "/api/settings/llm",
        json={"tasks": {"vision.ocr": {"provider": "qwen3-vl-30b", "reasoning_effort": "low"}}},
    )
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in resp.json()["tasks"]}
    assert tasks["vision.ocr"]["provider"] == "qwen3-vl-30b"
    # Local models take no reasoning level — it drops from the stored shape.
    assert tasks["vision.ocr"]["reasoning_effort"] is None
    stored = cast(dict[str, object], store.values["llm_task_overrides"])
    assert stored["vision.ocr"] == {"spec": "local:qwen3-vl-30b-a3b"}


def test_put_accepts_non_grok_provider_without_reasoning_effort() -> None:
    # The screen sends just `{provider}` for non-reasoning providers (local
    # models, Claude) — no reasoning_effort. The request model must accept that;
    # requiring the field 422s every non-grok save before the handler runs.
    settings = _cloud_settings(local_llm_enabled=True, local_models=["qwen3-vl-30b"])
    c, store = _authed_client(settings)
    # Local model with no effort (exactly the frontend's wire shape).
    resp = c.put("/api/settings/llm", json={"tasks": {"agent.turn": {"provider": "qwen3-vl-30b"}}})
    assert resp.status_code == 200, resp.text
    stored = cast(dict[str, object], store.values["llm_task_overrides"])
    assert stored["agent.turn"] == {"spec": "local:qwen3-vl-30b-a3b"}
    # Claude with no effort persists too (the other non-grok provider).
    assert (
        c.put(
            "/api/settings/llm", json={"tasks": {"note.extract": {"provider": "claude"}}}
        ).status_code
        == 200
    )
    # Grok with no effort falls back to the default rather than storing null.
    assert (
        c.put("/api/settings/llm", json={"tasks": {"agent.turn": {"provider": "grok"}}}).status_code
        == 200
    )
    stored = cast(dict[str, object], store.values["llm_task_overrides"])
    assert stored["agent.turn"] == {"spec": "xai:grok-4.3", "reasoning_effort": "low"}


def test_drawer_catalog_present_with_enabled_flags() -> None:
    # Off by default: the catalog still ships (so the drawer can show what's
    # available) but nothing is enabled.
    c, _ = _authed_client(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    body = c.get("/api/settings/llm").json()
    assert body["local_hosting_enabled"] is False
    by_id = {m["id"]: m for m in body["local_models"]}
    assert "qwen3-vl-30b" in by_id and "gpt-oss-120b" in by_id
    assert all(m["enabled"] is False for m in body["local_models"])
    assert by_id["qwen3-vl-30b"]["supports_vision"] is True

    # Enabled + a selection: only the selected catalog model reads enabled.
    c2, _ = _authed_client(
        Settings(
            secure_cookies=False,
            database_url="postgresql+asyncpg://nobody@localhost:1/none",
            local_llm_enabled=True,
            local_models=["gpt-oss-120b"],
        )
    )
    body2 = c2.get("/api/settings/llm").json()
    assert body2["local_hosting_enabled"] is True
    by_id2 = {m["id"]: m for m in body2["local_models"]}
    assert by_id2["gpt-oss-120b"]["enabled"] is True
    assert by_id2["qwen3-vl-30b"]["enabled"] is False
    assert by_id2["gpt-oss-120b"]["tiers"] == ["high"]


def test_put_rejects_local_model_when_hosting_disabled() -> None:
    c, store = _authed_client(
        Settings(
            secure_cookies=False,
            database_url="postgresql+asyncpg://nobody@localhost:1/none",
        )
    )
    # A real catalog id, but unreachable because local hosting is off.
    resp = c.put(
        "/api/settings/llm",
        json={"tasks": {"vision.ocr": {"provider": "qwen3-vl-30b", "reasoning_effort": "low"}}},
    )
    assert resp.status_code == 422
    assert "llm_task_overrides" not in store.values


def test_put_rejects_text_only_model_for_a_vision_task() -> None:
    # gpt-oss is enabled but text-only; routing a vision task to it must 422 even
    # though the provider id is otherwise valid (the UI filters this; the API
    # enforces it so a direct PUT can't send images to a blind model).
    c, store = _authed_client(
        Settings(
            secure_cookies=False,
            database_url="postgresql+asyncpg://nobody@localhost:1/none",
            local_llm_enabled=True,
            local_models=["qwen3-vl-30b", "gpt-oss-120b"],
        )
    )
    resp = c.put(
        "/api/settings/llm",
        json={"tasks": {"vision.ocr": {"provider": "gpt-oss-120b", "reasoning_effort": "low"}}},
    )
    assert resp.status_code == 422
    assert "llm_task_overrides" not in store.values
    # The vision-capable local model is still accepted for the same task.
    ok = c.put(
        "/api/settings/llm",
        json={"tasks": {"vision.ocr": {"provider": "qwen3-vl-30b", "reasoning_effort": "low"}}},
    )
    assert ok.status_code == 200


def test_get_surfaces_a_pinned_local_model_after_hosting_disabled() -> None:
    # Pin vision.ocr to a local model, then turn hosting OFF: the stored override
    # reverse-maps to no menu id, so the GET surfaces the raw provider half rather
    # than crashing, with no reasoning level.
    enabled = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b"],
    )
    c, store = _authed_client(enabled)
    assert (
        c.put(
            "/api/settings/llm",
            json={"tasks": {"vision.ocr": {"provider": "qwen3-vl-30b", "reasoning_effort": "low"}}},
        ).status_code
        == 200
    )
    overrides = store.values["llm_task_overrides"]

    # Same stored overrides, but a settings object with hosting off.
    c2, store2 = _authed_client(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    store2.values["llm_task_overrides"] = overrides
    tasks = {t["id"]: t for t in c2.get("/api/settings/llm").json()["tasks"]}
    assert tasks["vision.ocr"]["provider"] == "local"  # bare spec half, off-menu
    assert tasks["vision.ocr"]["reasoning_effort"] is None


def _local_settings() -> Settings:
    return _cloud_settings(local_llm_enabled=True, local_models=["qwen3-vl-30b", "gpt-oss-120b"])


def test_loaded_status_reflects_the_gateway() -> None:
    # The gateway reports qwen's served_model resident; the drawer marks that
    # catalog id loaded and everything else idle.
    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b"})
    c, _ = _authed_client(_local_settings(), gw)
    body = c.get("/api/settings/llm").json()
    by_id = {m["id"]: m for m in body["local_models"]}
    assert by_id["qwen3-vl-30b"]["loaded"] is True
    assert by_id["gpt-oss-120b"]["loaded"] is False
    # Memory meter is populated when hosting is on (Linux/CI); tolerate off-Linux.
    mem = body["host_memory"]
    if mem is not None:
        assert mem["total_gb"] > 0 and mem["used_gb"] >= 0


def test_loaded_status_is_false_when_gateway_unreachable() -> None:
    # FakeLocalGateway with empty running stands in for an unreachable/cold gateway
    # — best-effort, the screen still renders with nothing loaded.
    c, _ = _authed_client(_local_settings(), FakeLocalGateway())
    assert all(not m["loaded"] for m in c.get("/api/settings/llm").json()["local_models"])


def test_unload_evicts_the_model_and_returns_remaining_loaded() -> None:
    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b", "gpt-oss-120b"})
    c, _ = _authed_client(_local_settings(), gw)
    resp = c.post("/api/settings/llm/local-models/qwen3-vl-30b/unload")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["loaded"] == ["gpt-oss-120b"]  # qwen evicted
    assert gw.unloaded == ["qwen3-vl-30b-a3b"]  # called with the served_model


def test_unload_unknown_or_unprovisioned_model_404() -> None:
    c, _ = _authed_client(_local_settings(), FakeLocalGateway())
    # Not a catalog id at all.
    assert c.post("/api/settings/llm/local-models/nope/unload").status_code == 404
    # A real catalog id that wasn't provisioned in this install.
    assert c.post("/api/settings/llm/local-models/llama-3.3-70b/unload").status_code == 404


def test_unload_when_hosting_disabled_409() -> None:
    c, _ = _authed_client(_cloud_settings())  # local hosting off
    assert c.post("/api/settings/llm/local-models/qwen3-vl-30b/unload").status_code == 409


def test_unload_surfaces_a_gateway_failure_as_502() -> None:
    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b"}, fail_unload=True)
    c, _ = _authed_client(_local_settings(), gw)
    assert c.post("/api/settings/llm/local-models/qwen3-vl-30b/unload").status_code == 502


def test_unload_requires_auth() -> None:
    app = create_app(_local_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        app.state.local_gateway = FakeLocalGateway()
        assert anon.post("/api/settings/llm/local-models/qwen3-vl-30b/unload").status_code == 401
