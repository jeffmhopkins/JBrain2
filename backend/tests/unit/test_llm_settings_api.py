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
from jbrain.llm.residency import ResidencyCoordinator
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
    # Every routed task lists with the grok default. Effort now follows the task's
    # reasoning bucket (right-by-default), not a single global level: the arbiters
    # default high, the one-shots low, everything else medium; a vision task on a
    # reasoning-capable cloud provider falls back to the global default.
    assert {t["id"] for t in body["tasks"]} == set(TASK_DEFAULTS)
    effort = {t["id"]: t["reasoning_effort"] for t in body["tasks"]}
    assert all(t["provider"] == "grok" for t in body["tasks"])
    assert effort["integrate.note"] == "high"
    assert effort["fact.adjudicate"] == "high"
    assert effort["wiki.ground"] == "high"
    assert effort["agent.turn"] == "medium"
    assert effort["note.extract"] == "medium"
    assert effort["video.summarize"] == "medium"
    assert effort["entity.disambiguate"] == "low"
    assert effort["session.title"] == "low"
    assert effort["triage.classify"] == "low"
    # Vision tasks have no bucket effort; on grok (reasoning-capable) they show the
    # global fallback default.
    assert effort["vision.ocr"] == "low"


def test_jcode_section_defaults_disabled(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    # Code mode off (default settings) → the card is hidden (enabled False) and the
    # dropdown is empty, but the config default is still reported.
    c, _ = client
    jc = c.get("/api/settings/llm").json()["jcode"]
    assert jc["enabled"] is False
    assert jc["options"] == []
    assert jc["default"] == "qwen3-coder-next"
    assert jc["model"] == "qwen3-coder-next"
    # The planner half defaults to the config split default (the reasoner); the "same"
    # sentinel the card uses for its single-model option is surfaced so client + server agree.
    assert jc["planner"] == "gpt-oss-120b"
    assert jc["planner_default"] == "gpt-oss-120b"
    assert jc["planner_same"] == "same"


def test_jcode_model_selector_lists_installed_tool_capable_and_round_trips() -> None:
    # Hosting on with one installed tool-capable model (qwen3-vl-30b) → it's the sole
    # dropdown option; the default (qwen3-coder-next, not installed) is the effective
    # model until the owner picks one.
    c, _ = _authed_client(
        _cloud_settings(jcode_enabled=True, local_llm_enabled=True, local_models=["qwen3-vl-30b"])
    )
    jc = c.get("/api/settings/llm").json()["jcode"]
    assert jc["enabled"] is True
    assert {o["id"] for o in jc["options"]} == {"qwen3-vl-30b"}
    assert jc["model"] == "qwen3-coder-next"  # default, no override yet

    picked = c.put("/api/settings/llm/jcode-model", json={"model": "qwen3-vl-30b"})
    assert picked.status_code == 200
    assert picked.json()["jcode"]["model"] == "qwen3-vl-30b"

    # An id that isn't an installed, tool-capable model is rejected.
    assert c.put("/api/settings/llm/jcode-model", json={"model": "nope"}).status_code == 422

    # "" reverts to the config default.
    reset = c.put("/api/settings/llm/jcode-model", json={"model": ""})
    assert reset.json()["jcode"]["model"] == "qwen3-coder-next"


def test_jcode_planner_selector_round_trips_and_takes_the_same_sentinel() -> None:
    # The planner half of the card: it offers the same installed set plus the "same"
    # single-model sentinel, defaults to the config split planner, and round-trips.
    c, _ = _authed_client(
        _cloud_settings(jcode_enabled=True, local_llm_enabled=True, local_models=["qwen3-vl-30b"])
    )
    jc = c.get("/api/settings/llm").json()["jcode"]
    # No override yet → the config split default (the reasoner, even if not installed).
    assert jc["planner"] == "gpt-oss-120b"

    # Pick an installed model as the planner.
    picked = c.put("/api/settings/llm/jcode-planner", json={"planner": "qwen3-vl-30b"})
    assert picked.status_code == 200
    assert picked.json()["jcode"]["planner"] == "qwen3-vl-30b"

    # The "same" sentinel is accepted (single-model — the executor plans too) and preserved.
    same = c.put("/api/settings/llm/jcode-planner", json={"planner": "same"})
    assert same.status_code == 200
    assert same.json()["jcode"]["planner"] == "same"

    # A junk id (neither installed nor the sentinel) is rejected.
    assert c.put("/api/settings/llm/jcode-planner", json={"planner": "nope"}).status_code == 422

    # "" reverts to the config split default.
    reset = c.put("/api/settings/llm/jcode-planner", json={"planner": ""})
    assert reset.json()["jcode"]["planner"] == "gpt-oss-120b"


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
    # Residency over the SAME fake gateway, so the load/plan-load endpoints exercise the
    # real evictor against the test's running set (memory is monkeypatched per test).
    app.state.residency = ResidencyCoordinator(
        app.state.local_gateway, enabled=settings.local_llm_enabled, models_dir=""
    )
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


def test_put_persists_reasoning_effort_for_a_local_reasoning_model() -> None:
    # A reasoning-capable local model (gpt-oss) must keep its effort end to end —
    # stored, echoed in the effective task, and re-read — so the UI segment shows
    # selected and the router can send it. (Regression: this was grok-only.)
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b"])
    c, store = _authed_client(settings)
    resp = c.put(
        "/api/settings/llm",
        json={"tasks": {"agent.turn": {"provider": "gpt-oss-120b", "reasoning_effort": "high"}}},
    )
    assert resp.status_code == 200, resp.text
    tasks = {t["id"]: t for t in resp.json()["tasks"]}
    assert tasks["agent.turn"]["provider"] == "gpt-oss-120b"
    assert tasks["agent.turn"]["reasoning_effort"] == "high"
    stored = cast(dict[str, object], store.values["llm_task_overrides"])
    assert stored["agent.turn"] == {"spec": "local:gpt-oss-120b", "reasoning_effort": "high"}
    # A fresh GET reflects the stored effort (the screen highlights the segment).
    got = {t["id"]: t for t in c.get("/api/settings/llm").json()["tasks"]}
    assert got["agent.turn"]["reasoning_effort"] == "high"


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


def test_disk_gb_reports_the_real_footprint_when_provisioned(tmp_path: Any) -> None:
    # Lay down real weights for one provisioned model; the other isn't on disk.
    qwen = tmp_path / "qwen3-vl-30b"
    qwen.mkdir()
    (qwen / "model.gguf").write_bytes(b"\0" * (2 * 1024**3))
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b", "gpt-oss-120b"],
        local_models_dir=str(tmp_path),
    )
    c, _ = _authed_client(settings)
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    # Measured from the GGUF on disk, not the catalog estimate.
    assert by_id["qwen3-vl-30b"]["disk_gb"] == 2.0
    # Not provisioned here → null, so the screen falls back to the estimate.
    assert by_id["gpt-oss-120b"]["disk_gb"] is None


def test_disk_gb_is_null_when_hosting_disabled() -> None:
    c, _ = _authed_client(_cloud_settings())  # hosting off → never touch the disk
    assert all(m["disk_gb"] is None for m in c.get("/api/settings/llm").json()["local_models"])


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


def test_drawer_reports_context_window_fields() -> None:
    # Defaults: each model reports its catalog window and no override.
    c, _ = _authed_client(_local_settings())
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    assert by_id["gpt-oss-120b"]["context_window"] == 131072  # served default == native
    assert by_id["qwen3-vl-30b"]["context_window"] == 32768  # catalog default
    # The native ceiling the picker caps at — above the conservative served default.
    assert by_id["gpt-oss-120b"]["max_context_window"] == 131072
    assert by_id["qwen3-vl-30b"]["max_context_window"] == 262144
    assert all(m["context_window_override"] is None for m in by_id.values())
    # `staged` is gone from the wire — staging is now a transient client-side preview.
    assert all("staged" not in m for m in by_id.values())


def test_set_context_window_round_trips_override() -> None:
    c, store = _authed_client(_local_settings())
    resp = c.put(
        "/api/settings/llm/local-models/gpt-oss-120b/context-window",
        json={"context_window": 65536},
    )
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["gpt-oss-120b"]["context_window_override"] == 65536
    assert by_id["gpt-oss-120b"]["context_window"] == 131072  # the max is unchanged
    assert store.values["llm_local_context_windows"] == {"gpt-oss-120b": 65536}
    # Clearing with null reverts to the catalog default.
    resp = c.put(
        "/api/settings/llm/local-models/gpt-oss-120b/context-window",
        json={"context_window": None},
    )
    assert resp.status_code == 200
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["gpt-oss-120b"]["context_window_override"] is None
    assert store.values["llm_local_context_windows"] == {}


def test_set_context_window_rejects_a_window_over_the_models_max() -> None:
    c, store = _authed_client(_local_settings())
    # gpt-oss native max is 131072 — 256k exceeds it.
    assert (
        c.put(
            "/api/settings/llm/local-models/gpt-oss-120b/context-window",
            json={"context_window": 262144},
        ).status_code
        == 422
    )
    # qwen3-vl serves a 32k default but its native window is 256k — a value above
    # native (here 300k) is still rejected.
    assert (
        c.put(
            "/api/settings/llm/local-models/qwen3-vl-30b/context-window",
            json={"context_window": 300000},
        ).status_code
        == 422
    )
    # Zero/negative are rejected too.
    assert (
        c.put(
            "/api/settings/llm/local-models/gpt-oss-120b/context-window",
            json={"context_window": 0},
        ).status_code
        == 422
    )
    assert "llm_local_context_windows" not in store.values  # nothing leaked


def test_set_context_window_allows_above_the_served_default_up_to_native() -> None:
    # The drawer caps at the model's NATIVE window, not the conservative served
    # default — so an operator can opt into a bigger -c the weights support. qwen3-vl
    # serves 32k by default but accepts up to its 256k native window.
    c, store = _authed_client(_local_settings())
    resp = c.put(
        "/api/settings/llm/local-models/qwen3-vl-30b/context-window",
        json={"context_window": 131072},
    )
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["qwen3-vl-30b"]["context_window_override"] == 131072
    assert store.values["llm_local_context_windows"] == {"qwen3-vl-30b": 131072}


def test_set_context_window_404_and_409() -> None:
    c, _ = _authed_client(_local_settings())
    assert (
        c.put(
            "/api/settings/llm/local-models/nope/context-window",
            json={"context_window": 16384},
        ).status_code
        == 404
    )
    # hosting off → 409
    c2, _ = _authed_client(_cloud_settings())
    assert (
        c2.put(
            "/api/settings/llm/local-models/gpt-oss-120b/context-window",
            json={"context_window": 16384},
        ).status_code
        == 409
    )


def test_set_context_window_unloads_a_resident_model() -> None:
    # A new -c only applies on reload, so editing a loaded model's window evicts it
    # (its next request reloads at the new size).
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    c, _ = _authed_client(_local_settings(), gw)
    resp = c.put(
        "/api/settings/llm/local-models/gpt-oss-120b/context-window",
        json={"context_window": 65536},
    )
    assert resp.status_code == 200
    assert gw.unloaded == ["gpt-oss-120b"]  # evicted so it reloads at 64k


def test_plan_load_previews_the_eviction_without_touching_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (63.5) resident, used=90; staging the coder would blow the 96 ceiling. The
    # dry-run names gpt-oss as the victim (with its footprint), projects the landing point,
    # and evicts NOTHING. (qwen3-235b is provisioned so it's a valid plan-load target.)
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    settings = _cloud_settings(
        local_llm_enabled=True, local_models=["qwen3-coder-next", "gpt-oss-120b"]
    )
    c, _ = _authed_client(settings, gw)
    resp = c.post("/api/settings/llm/local-models/qwen3-coder-next/plan-load")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["measured"] is True
    assert body["fits"] is False and body["over"] is False and body["already_resident"] is False
    assert [v["id"] for v in body["victims"]] == ["gpt-oss-120b"]
    assert body["victims"][0]["gb"] == 63.5
    assert body["ceiling_gb"] == 96.0
    assert gw.unloaded == []  # dry-run — nothing evicted


def test_plan_load_fits_when_there_is_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 40.0)
    )
    c, _ = _authed_client(_local_settings(), FakeLocalGateway(running={"gpt-oss-120b"}))
    body = c.post("/api/settings/llm/local-models/qwen3-vl-30b/plan-load").json()
    assert body["fits"] is True and body["victims"] == []


def test_plan_load_is_unmeasured_when_memory_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A box that can't be measured → measured False, so the screen offers the load without an
    # eviction preview rather than showing a wrong one.
    monkeypatch.setattr("jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": None)
    c, _ = _authed_client(_local_settings())
    body = c.post("/api/settings/llm/local-models/qwen3-vl-30b/plan-load").json()
    assert body["measured"] is False and body["victims"] == []


def test_plan_load_404_and_409() -> None:
    c, _ = _authed_client(_local_settings())
    assert c.post("/api/settings/llm/local-models/nope/plan-load").status_code == 404
    c2, _ = _authed_client(_cloud_settings())
    assert c2.post("/api/settings/llm/local-models/gpt-oss-120b/plan-load").status_code == 409


def test_load_evicts_to_fit_then_warms_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Committing the staged load: free_room evicts the same victim the preview showed, then the
    # target is warmed. gpt-oss (63.5) resident at used=90; loading the coder evicts gpt-oss.
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    settings = _cloud_settings(
        local_llm_enabled=True, local_models=["qwen3-coder-next", "gpt-oss-120b"]
    )
    c, _ = _authed_client(settings, gw)
    resp = c.post("/api/settings/llm/local-models/qwen3-coder-next/load")
    assert resp.status_code == 200, resp.text
    assert gw.unloaded == ["gpt-oss-120b"]  # evicted to make room
    assert "qwen3-coder-next" in gw.loaded  # then warmed


def test_install_queues_an_unprovisioned_model() -> None:
    # qwen3-235b-a22b is in the catalog but not in this install's local_models, so
    # it can be queued for provisioning from the PWA.
    c, store = _authed_client(_local_settings())
    resp = c.post("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["qwen3-235b-a22b"]["queued"] is True
    assert by_id["qwen3-235b-a22b"]["enabled"] is False
    # An already-provisioned model is never marked queued.
    assert by_id["gpt-oss-120b"]["queued"] is False
    assert store.values["llm_local_provision_requested"] == ["qwen3-235b-a22b"]
    # Queuing again is idempotent (no duplicate).
    c.post("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    assert store.values["llm_local_provision_requested"] == ["qwen3-235b-a22b"]


def test_cancel_install_removes_from_the_queue_and_tolerates_absence() -> None:
    c, store = _authed_client(_local_settings())
    c.post("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    resp = c.delete("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["qwen3-235b-a22b"]["queued"] is False
    assert store.values["llm_local_provision_requested"] == []
    # Cancelling something not queued reconciles rather than 404 (a concurrent
    # update may have just provisioned and cleared it).
    assert c.delete("/api/settings/llm/local-models/glm-4.5-air/install").status_code == 200


def test_install_404_unknown_and_409_already_provisioned_or_hosting_off() -> None:
    c, _ = _authed_client(_local_settings())
    # Not a catalog id.
    assert c.post("/api/settings/llm/local-models/nope/install").status_code == 404
    # Already provisioned in this install → nothing to queue.
    assert c.post("/api/settings/llm/local-models/gpt-oss-120b/install").status_code == 409
    # Hosting off → the GPU/gateway env is a one-time host step the PWA can't bootstrap.
    c2, _ = _authed_client(_cloud_settings())
    assert c2.post("/api/settings/llm/local-models/qwen3-235b-a22b/install").status_code == 409


def test_install_download_progress_climbs_with_on_disk_bytes(tmp_path: Any) -> None:
    # A queued model mid-download reports download_gb from the bytes on disk (partial
    # shards included), so the drawer can render download_gb / size_gb as a live bar.
    model_dir = tmp_path / "qwen3-235b-a22b"
    model_dir.mkdir()
    # Sparse files so the GiB sizes cost no disk (st_size is all dir_size_gb reads).
    for name, size in (
        ("shard-00001-of-00003.gguf", 1024**3),
        ("shard-00002.gguf.incomplete", 1024**3 // 2),
    ):
        with (model_dir / name).open("wb") as f:
            f.truncate(size)
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b", "gpt-oss-120b"],
        local_models_dir=str(tmp_path),
    )
    c, _ = _authed_client(settings)
    c.post("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    assert by_id["qwen3-235b-a22b"]["download_gb"] == 1.5
    # A model with nothing on disk reports null, not 0 — the drawer shows "queued".
    assert by_id["glm-4.5-air"]["download_gb"] is None


def test_uninstall_queues_a_provisioned_model() -> None:
    # gpt-oss-120b is provisioned in this install, so it can be queued for uninstall.
    c, store = _authed_client(_local_settings())
    resp = c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["gpt-oss-120b"]["remove_queued"] is True
    assert by_id["gpt-oss-120b"]["enabled"] is True
    # An un-provisioned catalog model is never marked remove_queued.
    assert by_id["qwen3-235b-a22b"]["remove_queued"] is False
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]
    # Queuing again is idempotent (no duplicate).
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]


def test_uninstall_404_unknown_and_409_unprovisioned_or_hosting_off() -> None:
    c, _ = _authed_client(_local_settings())
    # Not a catalog id.
    assert c.post("/api/settings/llm/local-models/nope/uninstall").status_code == 404
    # A catalog model that isn't provisioned here → nothing to uninstall.
    assert c.post("/api/settings/llm/local-models/qwen3-235b-a22b/uninstall").status_code == 409
    # Hosting off → no local roster to uninstall from.
    c2, _ = _authed_client(_cloud_settings())
    assert c2.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall").status_code == 409


def test_uninstall_a_disabled_model_with_orphaned_weights_is_allowed(tmp_path: Any) -> None:
    # qwen3-235b-a22b is NOT in the roster, but its weights are orphaned on disk (an alt
    # the sync's roster recompute dropped). The drawer must still queue their removal —
    # the sync prunes any remove-queue id regardless of the roster. Lay down a real .gguf
    # so _disk_gb sees it.
    orphan = tmp_path / "qwen3-235b-a22b"
    orphan.mkdir()
    (orphan / "model.gguf").write_bytes(b"\0" * (2 * 1024**3))
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["gpt-oss-120b"],  # 235b intentionally absent from the roster
        local_models_dir=str(tmp_path),
    )
    c, store = _authed_client(settings)
    resp = c.post("/api/settings/llm/local-models/qwen3-235b-a22b/uninstall")
    assert resp.status_code == 200, resp.text
    assert store.values["llm_local_remove_requested"] == ["qwen3-235b-a22b"]


def test_uninstall_409_when_neither_enabled_nor_on_disk(tmp_path: Any) -> None:
    # An empty models dir → no orphaned weights, so a catalog id outside the roster has
    # nothing to remove and still 409s (the gate opens only for enabled OR on-disk).
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["gpt-oss-120b"],
        local_models_dir=str(tmp_path),
    )
    c, _ = _authed_client(settings)
    assert c.post("/api/settings/llm/local-models/qwen3-235b-a22b/uninstall").status_code == 409


def test_cancel_uninstall_removes_from_the_queue_and_tolerates_absence() -> None:
    c, store = _authed_client(_local_settings())
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    resp = c.delete("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["gpt-oss-120b"]["remove_queued"] is False
    assert store.values["llm_local_remove_requested"] == []
    # Cancelling something not queued reconciles rather than 404 (a concurrent
    # update may have just removed and cleared it).
    assert c.delete("/api/settings/llm/local-models/qwen3-vl-30b/uninstall").status_code == 200


def test_install_and_uninstall_queues_are_disjoint() -> None:
    # An id can't sit in both queues; queueing one strips the other so the sync's
    # set algebra stays unambiguous.
    c, store = _authed_client(_local_settings())
    # qwen3-235b-a22b is unprovisioned → installable; queue it, then uninstall a
    # provisioned model, then re-install/uninstall the SAME id to prove the swap.
    c.post("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    assert store.values["llm_local_provision_requested"] == ["qwen3-235b-a22b"]
    # gpt-oss-120b is provisioned: queue uninstall, then (hypothetically) install —
    # but install requires unprovisioned, so use the unprovisioned id for the swap.
    # First: uninstall gpt-oss-120b, then install qwen3-235b stays untouched.
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]
    assert store.values["llm_local_provision_requested"] == ["qwen3-235b-a22b"]
    # Now force a collision on the SAME id by seeding the remove queue with an
    # installable id, then installing it: the install must strip it from removing.
    store.values["llm_local_remove_requested"] = ["gpt-oss-120b", "qwen3-235b-a22b"]
    c.post("/api/settings/llm/local-models/qwen3-235b-a22b/install")
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]
    assert store.values["llm_local_provision_requested"] == ["qwen3-235b-a22b"]
    # And the reverse: seed the install queue with a provisioned id, uninstall it →
    # the uninstall strips it from the install queue.
    store.values["llm_local_provision_requested"] = ["qwen3-235b-a22b", "gpt-oss-120b"]
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert store.values["llm_local_provision_requested"] == ["qwen3-235b-a22b"]
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]


def test_remove_queued_self_clears_for_a_model_no_longer_provisioned() -> None:
    # The mirror of queued's self-clear: `remove_queued = removing and enabled`, so a
    # stale remove-queue entry for an id that already left LOCAL_MODELS (the update
    # applied the uninstall but a clear was missed) reports remove_queued False — the
    # row stops claiming "uninstalling" without waiting for the queue to be cleared.
    c, store = _authed_client(_local_settings())
    # qwen3-235b-a22b is NOT in local_models (unprovisioned), yet sits in the queue.
    store.values["llm_local_remove_requested"] = ["qwen3-235b-a22b"]
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    assert by_id["qwen3-235b-a22b"]["enabled"] is False
    assert by_id["qwen3-235b-a22b"]["remove_queued"] is False


def test_load_makes_the_model_resident() -> None:
    gw = FakeLocalGateway()
    c, _ = _authed_client(_local_settings(), gw)
    resp = c.post("/api/settings/llm/local-models/qwen3-vl-30b/load")
    assert resp.status_code == 200, resp.text
    assert gw.loaded == ["qwen3-vl-30b-a3b"]  # called with the served_model
    assert resp.json()["loaded"] == ["qwen3-vl-30b"]


def test_load_surfaces_a_gateway_failure_as_502() -> None:
    gw = FakeLocalGateway(fail_load=True)
    c, _ = _authed_client(_local_settings(), gw)
    assert c.post("/api/settings/llm/local-models/qwen3-vl-30b/load").status_code == 502


def test_load_404_and_409() -> None:
    c, _ = _authed_client(_local_settings())
    assert c.post("/api/settings/llm/local-models/nope/load").status_code == 404
    c2, _ = _authed_client(_cloud_settings())
    assert c2.post("/api/settings/llm/local-models/gpt-oss-120b/load").status_code == 409
