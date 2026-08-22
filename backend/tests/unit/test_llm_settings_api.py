"""The /api/settings/llm surface — runtime per-task LLM routing + reasoning
effort — with the store faked; the SQL store's round-trip is covered in
test_settings_pg."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from jbrain.api import llm_settings
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.llm import llama_swap_config, local_catalog, local_gateway
from jbrain.llm.residency import ResidencyCoordinator, ResidencyWiring
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
    # Every routed task lists EXCEPT the auto-title tasks, which are hidden from the
    # picker (they run on the chat's own model, not a separate route).
    assert {t["id"] for t in body["tasks"]} == set(TASK_DEFAULTS) - {
        "session.title",
        "research.title",
    }
    effort = {t["id"]: t["reasoning_effort"] for t in body["tasks"]}
    assert all(t["provider"] == "grok" for t in body["tasks"])
    assert effort["integrate.note"] == "high"
    assert effort["fact.adjudicate"] == "high"
    assert effort["wiki.ground"] == "high"
    assert effort["agent.turn"] == "medium"
    assert effort["note.extract"] == "medium"
    assert effort["video.summarize"] == "medium"
    assert effort["entity.disambiguate"] == "low"
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


def test_free_ram_fraction_defaults_to_config_and_round_trips(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, _ = client
    # No override stored → effective == the config default, override null.
    assert c.get("/api/settings/llm").json()["free_ram"] == {
        "fraction": 0.15,
        "default": 0.15,
        "override": None,
    }
    # Setting an override surfaces it as both the effective value and the override.
    picked = c.put("/api/settings/llm/free-ram-fraction", json={"fraction": 0.25})
    assert picked.status_code == 200
    assert picked.json()["free_ram"] == {"fraction": 0.25, "default": 0.15, "override": 0.25}
    # It persists across a fresh GET.
    assert c.get("/api/settings/llm").json()["free_ram"]["fraction"] == 0.25
    # null clears back to the config default.
    cleared = c.put("/api/settings/llm/free-ram-fraction", json={"fraction": None})
    assert cleared.json()["free_ram"] == {"fraction": 0.15, "default": 0.15, "override": None}


def test_auto_restore_defaults_on_and_round_trips(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    """The owner has no terminal, so the end-of-turn restore has to be switchable over the
    API the PWA calls. On by default (the long-standing behaviour); only an explicit off
    stops the box putting displaced models back."""
    c, _ = client
    assert c.get("/api/settings/llm").json()["auto_restore"] is True
    off = c.put("/api/settings/llm/auto-restore", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["auto_restore"] is False
    assert c.get("/api/settings/llm").json()["auto_restore"] is False
    back = c.put("/api/settings/llm/auto-restore", json={"enabled": True})
    assert back.json()["auto_restore"] is True


def test_free_ram_fraction_rejects_out_of_band_values(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, _ = client
    # Below 5% (invites the reclaim freeze) and above 50% (nothing worthwhile co-resides)
    # are refused, as are the (0, 1) edges; a non-number is a schema 422.
    for bad in (0.04, 0.51, 0.0, 1.0, -0.1):
        assert (
            c.put("/api/settings/llm/free-ram-fraction", json={"fraction": bad}).status_code == 422
        )
    assert (
        c.put("/api/settings/llm/free-ram-fraction", json={"fraction": "lots"}).status_code == 422
    )
    # The band edges are accepted.
    assert c.put("/api/settings/llm/free-ram-fraction", json={"fraction": 0.05}).status_code == 200
    assert c.put("/api/settings/llm/free-ram-fraction", json={"fraction": 0.5}).status_code == 200


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
        app.state.local_gateway,
        ResidencyWiring.inert(
            enabled=settings.local_llm_enabled,
            models_dir="",
            # Passed exactly as main.py / worker.py / router.py do. Omitting it left this
            # harness on the constructor default while every production site passed the
            # settings value, so these tests pinned a ceiling the box never used — 96.0 GB
            # against a real 108.8. Two defaults for one number is how a preview ends up
            # disagreeing with the evictor it is previewing.
            free_ram_fraction=settings.local_llm_free_ram_fraction,
        ),
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


def test_put_rejects_a_hidden_title_task() -> None:
    # The title tasks are hidden AND the router ignores their overrides; a direct PUT that
    # tries to pin one is refused (422) rather than silently stored and ignored.
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        xai_api_key="test-xai",
        anthropic_api_key="test-anthropic",
    )
    c, _ = _authed_client(settings)
    resp = c.put("/api/settings/llm", json={"tasks": {"session.title": {"provider": "grok"}}})
    assert resp.status_code == 422


def test_title_tasks_are_hidden_from_the_picker() -> None:
    # Auto-generated titles run on the chat's OWN model (jbrain.agent.titler passes the
    # turn's model), so they are not independently routable — the per-task picker must not
    # list them, or a stale pick would swap in a second model just to name a chat. They
    # stay in TASK_DEFAULTS (the router still routes them); only the settings surface hides.
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        xai_api_key="test-xai",
        anthropic_api_key="test-anthropic",
    )
    c, _ = _authed_client(settings)
    ids = {t["id"] for t in c.get("/api/settings/llm").json()["tasks"]}
    assert "session.title" not in ids and "research.title" not in ids
    assert "agent.turn" in ids  # the routable tasks are still offered


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


def test_drawer_reports_parallel_slots_default_of_one() -> None:
    c, _ = _authed_client(_local_settings())
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    assert all(m["parallel_slots"] == 1 for m in by_id.values())


def test_the_meter_reports_the_same_kv_the_eviction_budget_uses() -> None:
    """The meter must not re-derive the KV formula. It did, and the two silently diverged the
    moment `--swa-full` landed: footprint_gb doubled gpt-oss's KV and the meter did not, so the
    number the owner reads under-reported that model by 9 GB while the budget was right. A
    meter that disagrees with the budget is worse than no meter — it is the one an operator
    plans a load against."""
    c, _ = _authed_client(_local_settings())
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    expected = round(
        local_catalog.footprint_gb(model, model.context_window, disk_gb=0.0, slots=1), 2
    )
    assert by_id["gpt-oss-120b"]["kv_gb"] == expected
    # And it is genuinely the doubled KV figure plus the flat runtime term, not a coincidence
    # of the formulas agreeing. (`disk_gb=0.0` above zeroes the weights, so what is left is the
    # `--swa-full` doubled KV and the compute/output/state overhead every entry carries.)
    kv_doubled = model.kv_gb_per_128k * model.context_window / 131072 * 2
    assert expected == round(
        kv_doubled + local_catalog.RUNTIME_OVERHEAD_GB + local_catalog.CACHE_RAM_GB, 2
    )


def test_set_parallel_slots_round_trips_and_doubles_the_kv_estimate() -> None:
    c, store = _authed_client(_local_settings())
    base = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}["gpt-oss-120b"]
    resp = c.put("/api/settings/llm/local-models/gpt-oss-120b/parallel-slots", json={"slots": 2})
    assert resp.status_code == 200, resp.text
    m = {x["id"]: x for x in resp.json()["local_models"]}["gpt-oss-120b"]
    assert m["parallel_slots"] == 2
    # The meter reflects the doubled KV — and ONLY the KV. A second slot holds its own cache,
    # but the weights and the flat runtime term (compute/output buffers, recurrent state) are
    # shared, so the figure is not a plain doubling of the whole footprint.
    # The flat terms are the runtime overhead AND the in-RAM prompt cache: both are per-model,
    # not per-slot, so both stay outside the doubling.
    flat = local_catalog.RUNTIME_OVERHEAD_GB + local_catalog.CACHE_RAM_GB
    assert m["kv_gb"] == round((base["kv_gb"] - flat) * 2 + flat, 2)
    assert store.values["llm_local_parallel_slots"] == {"gpt-oss-120b": 2}
    # Clearing (1 or null) reverts to a single slot and drops the override row.
    resp = c.put("/api/settings/llm/local-models/gpt-oss-120b/parallel-slots", json={"slots": 1})
    m = {x["id"]: x for x in resp.json()["local_models"]}["gpt-oss-120b"]
    assert m["parallel_slots"] == 1
    assert store.values["llm_local_parallel_slots"] == {}


def test_set_parallel_slots_rejects_out_of_range() -> None:
    c, store = _authed_client(_local_settings())
    assert (
        c.put(
            "/api/settings/llm/local-models/gpt-oss-120b/parallel-slots", json={"slots": 3}
        ).status_code
        == 422
    )
    assert "llm_local_parallel_slots" not in store.values  # nothing leaked


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


async def test_a_changed_config_waits_for_the_gateway_to_reload_before_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A config change must settle BEFORE the caller's load, or the reload kills what it loads.

    MEASURED on the box: without this wait the gateway log reads `<gpt-oss-120b> Health check
    passed` / `<qwen3-vl-30b-a3b-q4> Health check passed` / `reloading configuration` — the load
    succeeds and is then silently killed by its own config change, taking the resident bystander
    with it. llama-swap's watcher polls on a 2 s interval, so the reload lands AFTER the model is
    already up.

    The first version of the load-time deferral had exactly this race: it fixed "editing kills
    everything" and replaced it with "loading an edited model kills everything, itself included".
    """
    slept: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        slept.append(secs)

    # Rebind the module's OWN `asyncio` name, not `asyncio.sleep` on the shared module object.
    # Patching the real `asyncio.sleep` hangs the suite: the app's background tick loops
    # (intake reaper, jpet, tasks) sleep between ticks, and a no-op sleep turns each of them into
    # a hot spin that starves the event loop. llm_settings uses asyncio for this call only.
    monkeypatch.setattr(llm_settings, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    cfg = tmp_path / "llama-swap.yaml"
    cfg.write_text("stale\n")
    rewrite = True

    def _fake_regen(*_a: object, **_k: object) -> None:
        if rewrite:  # a regen that genuinely rewrites the file — the reload-triggering case
            cfg.write_text("fresh\n")

    monkeypatch.setattr(llm_settings, "_try_regenerate", _fake_regen)
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b", "gpt-oss-120b"],
        local_models_dir=str(tmp_path),
    )
    await llm_settings.regen_gateway_config(settings, cast(Any, FakeSettingsStore()))
    assert slept == [llm_settings._GATEWAY_RELOAD_SETTLE_S], (
        "a changed config did not wait — the reload will land on the model just loaded"
    )

    # ...and the other direction: the wait is paid ONLY when something changed. Every load calls
    # this, and almost none of them change anything; charging 4 s to each would be a self-inflicted
    # tax on the common path.
    rewrite = False
    slept.clear()
    await llm_settings.regen_gateway_config(settings, cast(Any, FakeSettingsStore()))
    assert slept == [], "an unchanged config waited anyway — every load now pays for the race"


def test_editing_one_models_flags_never_rewrites_the_gateway_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug the owner reported repeatedly, and was repeatedly told was a display artifact:
    changing a dropdown on model A unloaded model B.

    Rewriting llama-swap.yaml makes llama-swap reload, and its reload calls `old.Shutdown()`,
    which kills EVERY running llama-server. `_try_regenerate` ran on all four settings PUTs, so
    editing a model that was not even loaded took down whatever was. Nothing in the app
    narrated it — the kill is inside llama-swap, so no `box_events` row is written — which is
    why it read as a phantom. Confirmed live: one image-detail change took gpt-oss-120b from
    resident to gone, llama-swap logged two reload cycles, and box_events recorded nothing.

    Asserts on the WRITE, not on the file's mtime. `_try_regenerate` is best-effort and
    silently swallows a render failure, and in a test with no resolvable weight files it fails
    every time — so an mtime assertion passes just as happily against the OLD eager-regen code,
    proving nothing. Spying on `llama_swap_config.write` is the only form of this test that
    actually fails when the regen comes back."""
    calls: list[str] = []
    monkeypatch.setattr(
        llama_swap_config, "write", lambda *a, **k: calls.append("write") or "/tmp/x.yaml"
    )
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    c, _ = _authed_client(_local_settings(), gw)

    # Edit a DIFFERENT, non-resident model — exactly the reported gesture.
    resp = c.put(
        "/api/settings/llm/local-models/qwen3-vl-30b/image-min-tokens",
        json={"image_min_tokens": 1024},
    )
    assert resp.status_code == 200
    assert calls == [], (
        "a settings PUT rewrote llama-swap.yaml — the gateway will reload and kill every "
        "resident model. The re-stamp belongs at load time (LocalGatewayClient._config_regen)."
    )
    assert gw.unloaded == [], "editing one model's flags unloaded a different resident model"


def test_plan_load_previews_the_eviction_without_touching_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (68.55 — full-history KV + the flat runtime term) resident, used=90; staging the
    # ceiling. The
    # dry-run names gpt-oss as the victim (with its footprint), projects the landing point,
    # and evicts NOTHING. (qwen3-coder-next is provisioned so it's a valid plan-load target.)
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
    # 68.5, not 76.5: the gateway serves `-cram 0`, so there is no in-RAM prompt cache term.
    assert body["victims"][0]["gb"] == 68.5
    # 128 GB * (1 - 0.15). Was asserted at 96.0 (i.e. 0.25) because the harness above
    # omitted the fraction; production has always used the settings value.
    assert body["ceiling_gb"] == 108.8
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


def test_set_available_toggles_the_roster_and_unloads_on_unavailable() -> None:
    # Both models provisioned + resident. Marking one unavailable drops it from the effective
    # roster (available False), keeps it installed (enabled True), and unloads it from memory.
    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b", "gpt-oss-120b"})
    c, store = _authed_client(_local_settings(), gw)
    resp = c.put("/api/settings/llm/local-models/qwen3-vl-30b/available", json={"available": False})
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["qwen3-vl-30b"]["enabled"] is True  # still installed
    assert by_id["qwen3-vl-30b"]["available"] is False  # out of the roster
    assert by_id["gpt-oss-120b"]["available"] is True  # untouched
    assert store.values["llm_local_unavailable"] == ["qwen3-vl-30b"]
    assert gw.unloaded == ["qwen3-vl-30b-a3b"]  # freed its memory

    # Making it available again clears the flag (no gateway action).
    resp = c.put("/api/settings/llm/local-models/qwen3-vl-30b/available", json={"available": True})
    assert {m["id"]: m for m in resp.json()["local_models"]}["qwen3-vl-30b"]["available"] is True
    assert store.values["llm_local_unavailable"] == []


def test_set_available_404_and_409() -> None:
    c, _ = _authed_client(_local_settings())
    unknown = c.put("/api/settings/llm/local-models/nope/available", json={"available": False})
    assert unknown.status_code == 404
    c2, _ = _authed_client(_cloud_settings())
    off = c2.put("/api/settings/llm/local-models/gpt-oss-120b/available", json={"available": False})
    assert off.status_code == 409


def test_plan_load_flags_an_over_box_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 20 GB box can't hold gpt-oss (68.55): the preview flags over_box so the screen can
    # disable Load.
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (20.0, 2.0)
    )
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b"])
    c, _ = _authed_client(settings, FakeLocalGateway())
    body = c.post("/api/settings/llm/local-models/gpt-oss-120b/plan-load").json()
    assert body["over_box"] is True and body["measured"] is True


def test_load_refuses_an_over_box_model_with_409(monkeypatch: pytest.MonkeyPatch) -> None:
    # Committing a load that can't fit the box is refused (409) and evicts NOTHING — loading it
    # would OOM-crash the box, so we never destroy resident models for it.
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (20.0, 6.0)
    )
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b", "qwen3.5-4b"])
    c, _ = _authed_client(settings, gw)
    resp = c.post("/api/settings/llm/local-models/gpt-oss-120b/load")
    assert resp.status_code == 409, resp.text
    assert gw.unloaded == []  # the resident tiny model is spared
    assert "gpt-oss-120b" not in gw.loaded  # never attempted


def test_load_evicts_to_fit_then_warms_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Committing the staged load: free_room evicts the same victim the preview showed, then the
    # target is warmed. gpt-oss (68.55) resident at used=90; loading the coder evicts gpt-oss.
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
    # nemotron-3-super-120b is in the catalog but not in this install's local_models, so
    # it can be queued for provisioning from the PWA.
    c, store = _authed_client(_local_settings())
    resp = c.post("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["nemotron-3-super-120b"]["queued"] is True
    assert by_id["nemotron-3-super-120b"]["enabled"] is False
    # An already-provisioned model is never marked queued.
    assert by_id["gpt-oss-120b"]["queued"] is False
    assert store.values["llm_local_provision_requested"] == ["nemotron-3-super-120b"]
    # Queuing again is idempotent (no duplicate).
    c.post("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    assert store.values["llm_local_provision_requested"] == ["nemotron-3-super-120b"]


def test_cancel_install_removes_from_the_queue_and_tolerates_absence() -> None:
    c, store = _authed_client(_local_settings())
    c.post("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    resp = c.delete("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["local_models"]}
    assert by_id["nemotron-3-super-120b"]["queued"] is False
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
    assert (
        c2.post("/api/settings/llm/local-models/nemotron-3-super-120b/install").status_code == 409
    )


def test_install_download_progress_climbs_with_on_disk_bytes(tmp_path: Any) -> None:
    # A queued model mid-download reports download_gb from the bytes on disk (partial
    # shards included), so the drawer can render download_gb / size_gb as a live bar.
    model_dir = tmp_path / "nemotron-3-super-120b"
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
    c.post("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    assert by_id["nemotron-3-super-120b"]["download_gb"] == 1.5
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
    assert by_id["nemotron-3-super-120b"]["remove_queued"] is False
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]
    # Queuing again is idempotent (no duplicate).
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]


def test_uninstall_404_unknown_and_409_unprovisioned_or_hosting_off() -> None:
    c, _ = _authed_client(_local_settings())
    # Not a catalog id.
    assert c.post("/api/settings/llm/local-models/nope/uninstall").status_code == 404
    # A catalog model that isn't provisioned here → nothing to uninstall.
    assert (
        c.post("/api/settings/llm/local-models/nemotron-3-super-120b/uninstall").status_code == 409
    )
    # Hosting off → no local roster to uninstall from.
    c2, _ = _authed_client(_cloud_settings())
    assert c2.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall").status_code == 409


def test_uninstall_a_disabled_model_with_orphaned_weights_is_allowed(tmp_path: Any) -> None:
    # nemotron-3-super-120b is NOT in the roster, but its weights are orphaned on disk (an alt
    # the sync's roster recompute dropped). The drawer must still queue their removal —
    # the sync prunes any remove-queue id regardless of the roster. Lay down a real .gguf
    # so _disk_gb sees it.
    orphan = tmp_path / "nemotron-3-super-120b"
    orphan.mkdir()
    (orphan / "model.gguf").write_bytes(b"\0" * (2 * 1024**3))
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["gpt-oss-120b"],  # 235b intentionally absent from the roster
        local_models_dir=str(tmp_path),
    )
    c, store = _authed_client(settings)
    resp = c.post("/api/settings/llm/local-models/nemotron-3-super-120b/uninstall")
    assert resp.status_code == 200, resp.text
    assert store.values["llm_local_remove_requested"] == ["nemotron-3-super-120b"]


def test_uninstall_409_when_neither_enabled_nor_on_disk(tmp_path: Any) -> None:
    # An empty models dir → no orphaned weights, so a catalog id outside the roster has
    # nothing to remove and still 409s (the gate opens only for enabled OR on-disk).
    settings = _cloud_settings(
        local_llm_enabled=True,
        local_models=["gpt-oss-120b"],
        local_models_dir=str(tmp_path),
    )
    c, _ = _authed_client(settings)
    assert (
        c.post("/api/settings/llm/local-models/nemotron-3-super-120b/uninstall").status_code == 409
    )


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
    # nemotron-3-super-120b is unprovisioned → installable; queue it, then uninstall a
    # provisioned model, then re-install/uninstall the SAME id to prove the swap.
    c.post("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    assert store.values["llm_local_provision_requested"] == ["nemotron-3-super-120b"]
    # gpt-oss-120b is provisioned: queue uninstall, then (hypothetically) install —
    # but install requires unprovisioned, so use the unprovisioned id for the swap.
    # First: uninstall gpt-oss-120b, then install nemotron-3-super-120b stays untouched.
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]
    assert store.values["llm_local_provision_requested"] == ["nemotron-3-super-120b"]
    # Now force a collision on the SAME id by seeding the remove queue with an
    # installable id, then installing it: the install must strip it from removing.
    store.values["llm_local_remove_requested"] = ["gpt-oss-120b", "nemotron-3-super-120b"]
    c.post("/api/settings/llm/local-models/nemotron-3-super-120b/install")
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]
    assert store.values["llm_local_provision_requested"] == ["nemotron-3-super-120b"]
    # And the reverse: seed the install queue with a provisioned id, uninstall it →
    # the uninstall strips it from the install queue.
    store.values["llm_local_provision_requested"] = ["nemotron-3-super-120b", "gpt-oss-120b"]
    c.post("/api/settings/llm/local-models/gpt-oss-120b/uninstall")
    assert store.values["llm_local_provision_requested"] == ["nemotron-3-super-120b"]
    assert store.values["llm_local_remove_requested"] == ["gpt-oss-120b"]


def test_remove_queued_self_clears_for_a_model_no_longer_provisioned() -> None:
    # The mirror of queued's self-clear: `remove_queued = removing and enabled`, so a
    # stale remove-queue entry for an id that already left LOCAL_MODELS (the update
    # applied the uninstall but a clear was missed) reports remove_queued False — the
    # row stops claiming "uninstalling" without waiting for the queue to be cleared.
    c, store = _authed_client(_local_settings())
    # nemotron-3-super-120b is NOT in local_models (unprovisioned), yet sits in the queue.
    store.values["llm_local_remove_requested"] = ["nemotron-3-super-120b"]
    by_id = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    assert by_id["nemotron-3-super-120b"]["enabled"] is False
    assert by_id["nemotron-3-super-120b"]["remove_queued"] is False


def test_load_makes_the_model_resident(monkeypatch: pytest.MonkeyPatch) -> None:
    # A generous box so the load fits without the over-box guard tripping on the (small) CI
    # container's real RAM — this test is about the load path, not eviction.
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 10.0)
    )
    gw = FakeLocalGateway()
    c, _ = _authed_client(_local_settings(), gw)
    resp = c.post("/api/settings/llm/local-models/qwen3-vl-30b/load")
    assert resp.status_code == 200, resp.text
    assert gw.loaded == ["qwen3-vl-30b-a3b"]  # called with the served_model
    assert resp.json()["loaded"] == ["qwen3-vl-30b"]
    # The warm-up is primed with the interactive persona (jerv) prompt so the first
    # conversation turn reuses that cached prefix instead of a cold prefill.
    from jbrain.agent.agents import AGENTS

    assert gw.warmed_system == [AGENTS["jerv"].prompt]


def test_load_surfaces_a_gateway_failure_as_502(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 10.0)
    )
    gw = FakeLocalGateway(fail_load=True)
    c, _ = _authed_client(_local_settings(), gw)
    assert c.post("/api/settings/llm/local-models/qwen3-vl-30b/load").status_code == 502


def test_load_404_and_409() -> None:
    c, _ = _authed_client(_local_settings())
    assert c.post("/api/settings/llm/local-models/nope/load").status_code == 404
    c2, _ = _authed_client(_cloud_settings())
    assert c2.post("/api/settings/llm/local-models/gpt-oss-120b/load").status_code == 409


def test_extra_arg_allowlist_accepts_flags_with_their_values() -> None:
    # An ALLOWLIST, not a filter: llama-server refuses to start on an unknown flag, and the flag
    # lands in that model's launch command — so an unrestricted argv could make a model
    # permanently unloadable on a box with no terminal. Values ride positionally.
    assert llm_settings._validate_extra_args(["--spec-draft-p-min", "0.6"]) == [
        "--spec-draft-p-min",
        "0.6",
    ]
    assert llm_settings._validate_extra_args(["--swa-full"]) == ["--swa-full"]  # boolean, no value
    assert llm_settings._validate_extra_args([]) == []  # clearing


def test_extra_arg_allowlist_covers_the_speculative_tuning_flags() -> None:
    # The right values for these are EMPIRICAL and hardware-specific (published Strix Halo
    # numbers disagree on n-max; p-min's payoff depends on generation length), and llama.cpp's
    # own p-min default is 0.00 — ungated. Without them on the allowlist a single tuning
    # iteration costs a catalog edit, a release and an Ops → Update, which is how a knob ends up
    # never tuned at all. Pinned so a future edit can't quietly drop the remote path.
    for flag in ("--spec-type", "--spec-draft-n-max", "--spec-draft-n-min", "--spec-draft-p-min"):
        assert flag in llm_settings.EXTRA_ARG_FLAGS
        assert llm_settings._validate_extra_args([flag, "x"]) == [flag, "x"]


def test_extra_arg_allowlist_covers_the_image_token_flags() -> None:
    """The image FLOOR decides whether small text in a photo survives to the model — raise it
    and OCR on a curved label or a receipt gets legible, at the cost of prefill and KV — and no
    amount of reading says where that threshold sits for a given camera and subject. It is only
    observable against real images, so it has to be tunable without a release, exactly like the
    speculative flags beside it.

    The ceiling is the memory lever: llama.cpp defaults it to 4096 for this projector family
    and the catalog pins only the floor. It matters much less now that flash attention is
    confirmed on (the CLIP term is linear in patches, not quadratic), but it is the control if
    a build ever loses `-fa`."""
    for flag in ("--image-min-tokens", "--image-max-tokens"):
        assert flag in llm_settings.EXTRA_ARG_FLAGS
        assert llm_settings._validate_extra_args([flag, "2048"]) == [flag, "2048"]
    # Both at once is the realistic call — bracketing the encode from below and above.
    assert llm_settings._validate_extra_args(
        ["--image-min-tokens", "2048", "--image-max-tokens", "4096"]
    ) == ["--image-min-tokens", "2048", "--image-max-tokens", "4096"]


def test_the_cache_flags_are_settable_for_a_hybrids_slow_prefill() -> None:
    """`--ctx-checkpoints` and `--cache-reuse` are the two knobs a hybrid's prefill behaviour
    actually turns on, and both ship as hardcoded defaults tuned for memory rather than latency.

    Qwen3.8 runs 48 of its 65 layers as Gated DeltaNet, whose recurrent state cannot be
    KV-shifted: `--cache-reuse` reaches only the 16 attention layers, and checkpoints are the
    ONLY mid-sequence resume path. We serve `--ctx-checkpoints 2` (down from llama.cpp's 32, to
    save ~4.7 GiB/slot), which is close to none. Whether that trade is right is empirical about
    this box, and without these flags answering it costs a release."""
    for flag in ("--ctx-checkpoints", "--cache-reuse"):
        assert flag in llm_settings.EXTRA_ARG_FLAGS
    assert llm_settings._validate_extra_args(["--ctx-checkpoints", "8"]) == [
        "--ctx-checkpoints",
        "8",
    ]
    assert llm_settings._validate_extra_args(["--cache-reuse", "0"]) == ["--cache-reuse", "0"]


def test_ctx_checkpoints_is_bounded_because_its_bad_value_hangs_the_box() -> None:
    """The one flag on the list whose failure is not "the model does not load".

    Everything else fails recoverably — clearing does not require a loadable model. A checkpoint
    on a hybrid is a full copy of the recurrent state (~150 MiB for Qwen3.8), device-resident and
    per slot, and `footprint_gb` budgets it only at the SERVED count, not at whatever is set here,
    so everything above that is unbudgeted and the residency evictor cannot see it coming.
    llama.cpp's own default of 32 is the most likely typo (every upstream doc names it) and would
    be ~4.7 GiB/slot unbudgeted on a box whose documented failure mode is an unrecoverable hang."""
    # 32 is llama.cpp's own default and must be REACHABLE — an earlier 0..8 bound put the one
    # value most worth sweeping out of reach, which defeats the point of exposing the flag.
    assert llm_settings._validate_extra_args(["--ctx-checkpoints", "32"]) == [
        "--ctx-checkpoints",
        "32",
    ]
    with pytest.raises(HTTPException) as exc:
        llm_settings._validate_extra_args(["--ctx-checkpoints", "64"])
    assert exc.value.status_code == 422
    assert "hang" in str(exc.value.detail)
    with pytest.raises(HTTPException):
        llm_settings._validate_extra_args(["--ctx-checkpoints", "-1"])
    with pytest.raises(HTTPException):  # not an integer at all
        llm_settings._validate_extra_args(["--ctx-checkpoints", "lots"])
    # The bound is per-flag, not a blanket numeric rule: an unbounded flag still takes any value.
    assert llm_settings._validate_extra_args(["-ub", "4096"]) == ["-ub", "4096"]
    assert llm_settings._validate_extra_args(["--cache-reuse", "99999"]) == [
        "--cache-reuse",
        "99999",
    ]


def test_the_snapshot_reports_the_local_call_timeout() -> None:
    """Env-only, so it cannot be changed from the box — but it must at least be VISIBLE.

    A cold prefill at a large window can exceed it, and the turn then fails as a client timeout
    that presents as a hung model. An investigator who cannot see the ceiling cannot rule it out,
    and spends the day on the gateway instead."""
    out = llm_settings.LlmSettingsOut.model_fields
    assert "local_llm_timeout_s" in out


def test_the_gpu_bisect_and_reasoning_format_are_settable() -> None:
    """`-ngl`/`-fa` are the "is it the GPU?" bisect: when a model emits garbage or dies on this
    gfx1151 (the failure class behind our `-ub 1024`, llama.cpp #27237), the first diagnostic is
    fewer offloaded layers or flash attention off — and it was unavailable remotely. Neither can
    make a model unloadable; a wrong value costs speed or a CPU fallback.

    `--reasoning-format` covers the other common post-rebuild breakage — `<think>` leaking into
    `content`, or an empty reasoning channel — a one-string fix that otherwise costs a release.

    All three are also emitted by the shared command or the catalog, so they only work because
    an operator copy now REPLACES the base one rather than appending a second occurrence."""
    for flag, value in (("-ngl", "0"), ("-fa", "0"), ("--reasoning-format", "auto")):
        assert flag in llm_settings.EXTRA_ARG_FLAGS
        assert llm_settings._validate_extra_args([flag, value]) == [flag, value]


def test_no_mmap_stays_off_the_allowlist_because_an_entry_would_be_a_no_op() -> None:
    """Not an oversight. llama.cpp has no positive `--mmap`, so an allowlist entry could not
    undo the flag the shared command already passes — it would be a silent no-op, which is worse
    than an absent one. Pinned so nobody "completes" the list without noticing."""
    assert "--no-mmap" not in llm_settings.EXTRA_ARG_FLAGS
    assert "--jinja" not in llm_settings.EXTRA_ARG_FLAGS
    with pytest.raises(HTTPException):
        llm_settings._validate_extra_args(["--no-mmap"])


def test_flash_attention_cannot_be_disabled_on_a_vision_model() -> None:
    """The guard on the one allowlist entry that could hang the box.

    `-fa` is here for the "is it the GPU?" bisect, and on a TEXT-ONLY model turning it off is
    the cheap experiment it looks like. On a vision model it is not: llama.cpp then materialises
    the full [n_patches, n_patches] CLIP attention matrix instead of tiling it, so the workspace
    goes from the ~0.47 GB linear branch `_vision_resident_gb` assumes to ~16 GB — unbudgeted,
    and landing on the first full-resolution image, long after the load guard passed and the
    watchdog stopped watching.

    Refused rather than budgeted: threading the served `-fa` through `footprint_gb` is a real
    change to the memory model, and shipping the flag before that lands would put a host hang
    one API call away."""
    vision = local_catalog.get("qwen3.8-27b-q4")
    text_only = local_catalog.get("gpt-oss-120b")
    assert vision is not None and vision.mmproj_include
    assert text_only is not None and not text_only.mmproj_include

    for off in ("0", "off", "false", "OFF"):
        with pytest.raises(HTTPException) as exc:
            llm_settings._validate_extra_args(["-fa", off], vision)
        assert exc.value.status_code == 422
        assert "quadratic" in str(exc.value.detail)

    # Leaving it ON is fine on a vision model — that is the branch the budget models.
    assert llm_settings._validate_extra_args(["-fa", "1"], vision) == ["-fa", "1"]
    # And the bisect stays available where it is safe.
    assert llm_settings._validate_extra_args(["-fa", "0"], text_only) == ["-fa", "0"]
    # With no model in hand (a caller that cannot say), nothing is refused — the endpoint always
    # passes one, so this only affects direct calls.
    assert llm_settings._validate_extra_args(["-fa", "0"]) == ["-fa", "0"]


def test_the_image_ceiling_is_bounded_to_what_the_vision_budget_assumes() -> None:
    """`_vision_resident_gb` sizes the CLIP workspace at a hardcoded 4096 image tokens. Raising
    the ceiling past that grows a workspace the budget does not follow, so the cap is the figure
    the budget already assumes rather than an arbitrary limit."""
    assert llm_settings._validate_extra_args(["--image-max-tokens", "4096"]) == [
        "--image-max-tokens",
        "4096",
    ]
    with pytest.raises(HTTPException) as exc:
        llm_settings._validate_extra_args(["--image-max-tokens", "8192"])
    assert exc.value.status_code == 422


def test_the_next_prefill_investigation_can_reach_its_levers() -> None:
    """The flags this session needed and could not set.

    `-lv 4` is the decisive one and has no substitute: whether checkpoints are created and
    MATCHED is only visible in llama-server's TRC lines. Without it a checkpoint sweep cannot
    tell "the count is wrong" from "nothing is ever restored" — identical timings either way,
    which is how a 2-vs-8 sweep measured nothing and was misread as the flag being inert."""
    for flag, value in (("-lv", "4"), ("--checkpoint-min-step", "512")):
        assert flag in llm_settings.EXTRA_ARG_FLAGS
        assert llm_settings._validate_extra_args([flag, value]) == [flag, value]
    # Bounded: verbosity has no level 9.
    with pytest.raises(HTTPException) as exc:
        llm_settings._validate_extra_args(["-lv", "9"])
    assert exc.value.status_code == 422


def test_the_prompt_cache_cannot_be_turned_back_on_from_the_console() -> None:
    """`--cache-ram` was allowlisted, and must not be any more.

    The gateway serves `-cram 0` and `local_catalog.CACHE_RAM_GB` is 0.0 to match. An operator
    re-enabling the cache from the PWA would serve up to 32 GiB of host RAM that the residency
    budget believes does not exist — under-reserving on the one path this box has hard-locked
    on. The flag and the budget term are ONE decision; the allowlist must not be a second,
    unbudgeted way to move half of it.

    `--slot-save-path` goes with it: the KV-slot feature it configured is gone, so an entry
    would name a directory nothing reads and no volume provides."""
    for flag in ("--cache-ram", "--slot-save-path"):
        assert flag not in llm_settings.EXTRA_ARG_FLAGS
        with pytest.raises(HTTPException) as exc:
            llm_settings._validate_extra_args([flag, "16384"])
        assert exc.value.status_code == 422


def test_serving_metrics_expose_prompt_cache_reuse() -> None:
    """The authoritative reuse signal, and the reason it is not the obvious one.

    `/slots`' `n_prompt_tokens_cache` is zeroed when llama.cpp releases the slot, so polling it
    after a completed request reads 0 whether reuse was total or nonexistent — an entire
    investigation concluded a hybrid could not cache at all on exactly that reading. The
    cumulative counters survive release."""
    text = "\n".join(
        [
            "# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed",
            "llamacpp:prompt_tokens_total 4",
            "llamacpp:prompt_tokens_cached_total 32485",
            "llamacpp:n_decode_total 10",
            "llamacpp:tokens_predicted_total 24",
            "llamacpp:tokens_predicted_seconds_total 2.0",
        ]
    )
    out = local_gateway.parse_spec_counters(text)
    assert out["prompt_tokens_cached_total"] == 32485
    assert out["prompt_tokens_total"] == 4
    # ~99.99% reuse — the shape of a warm prime on this box, measured 2026-08-18.
    assert out["cache_hit_rate"] == pytest.approx(0.9999, abs=0.0001)
    # The speculation figures still come through unchanged.
    assert out["tokens_per_step"] == 2.4


def test_extra_arg_allowlist_rejects_an_unknown_flag_loudly() -> None:
    # 422, never a silent drop: a caller that believes it set a flag and did not would misread
    # every measurement taken afterwards.
    with pytest.raises(HTTPException) as exc:
        llm_settings._validate_extra_args(["--spec-draft-typo", "3"])
    assert exc.value.status_code == 422
    # A bare value with no flag in front of it is refused too, not silently swallowed.
    with pytest.raises(HTTPException):
        llm_settings._validate_extra_args(["0.6"])


def test_image_min_tokens_refuses_a_model_without_a_projector() -> None:
    """422, not a silent no-op. `--image-min-tokens` is only read by the vision path, so a
    floor saved against a text-only model would persist, render, and do nothing — and the
    operator would conclude the knob does not work rather than that it does not apply."""
    c, _ = _authed_client(_local_settings())
    r = c.put(
        "/api/settings/llm/local-models/gpt-oss-120b/image-min-tokens",
        json={"image_min_tokens": 2048},
    )
    assert r.status_code == 422
    assert "projector" in r.json()["detail"]


def test_image_min_tokens_is_bounded_by_what_the_engine_can_honour() -> None:
    """llama.cpp caps this projector family at 4096 (`set_limit_image_tokens(8, 4096)`), so a
    higher floor could never take effect and accepting one would promise detail the engine
    will not deliver."""
    c, _ = _authed_client(_local_settings())
    for bad in (0, -1, 8192):
        r = c.put(
            "/api/settings/llm/local-models/qwen3-vl-30b/image-min-tokens",
            json={"image_min_tokens": bad},
        )
        assert r.status_code == 422, bad


def test_image_min_tokens_round_trips_and_clears() -> None:
    """The snapshot reports the EFFECTIVE floor plus the catalog default beside it, so the
    drawer can mark the default and store null for it rather than a redundant override row."""
    c, _ = _authed_client(_local_settings())
    r = c.put(
        "/api/settings/llm/local-models/qwen3-vl-30b/image-min-tokens",
        json={"image_min_tokens": 2048},
    )
    assert r.status_code == 200
    m = next(x for x in r.json()["local_models"] if x["id"] == "qwen3-vl-30b")
    assert m["image_min_tokens"] == 2048
    # This entry passes no floor of its own, so there is no default to fall back to — the
    # drawer marks nothing "(default)" and the operator's value stands alone.
    assert m["image_min_tokens_default"] is None

    r = c.put(
        "/api/settings/llm/local-models/qwen3-vl-30b/image-min-tokens",
        json={"image_min_tokens": None},
    )
    m = next(x for x in r.json()["local_models"] if x["id"] == "qwen3-vl-30b")
    assert m["image_min_tokens"] is None  # cleared, and no catalog floor beneath it


def test_a_text_only_model_reports_no_image_floor_at_all() -> None:
    """None rather than a number, so the drawer renders no control instead of a dead one."""
    c, _ = _authed_client(_local_settings())
    body = c.get("/api/settings/llm").json()
    m = next(x for x in body["local_models"] if x["id"] == "gpt-oss-120b")
    assert m["image_min_tokens"] is None and m["image_min_tokens_default"] is None


def test_a_prime_puts_back_what_it_displaced_but_a_load_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two deliberate warms admit with DIFFERENT displacement semantics, and swapping them
    is silent — both evict the same victim and both return 200.

    A load is a steady-state change: the operator asked for this model, so the victim is gone
    until they say otherwise (`free_room`, no restore recorded). A prime is the measurement
    instrument for prefill experiments — run repeatedly, deliberately transient — so under
    `free_room` each run would quietly strip the box of whatever it displaced. `ensure_room`
    records the victim so the end-of-turn restore puts it back."""
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    from jbrain.api.llm_settings import _admit_or_409

    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    settings = _cloud_settings(
        local_llm_enabled=True, local_models=["qwen3-coder-next", "gpt-oss-120b"]
    )
    _c, _ = _authed_client(settings, gw)
    residency = ResidencyCoordinator(
        gw,
        ResidencyWiring.inert(enabled=True, free_ram_fraction=settings.local_llm_free_ram_fraction),
    )

    asyncio.run(_admit_or_409(residency, "qwen3-coder-next"))
    assert gw.unloaded == ["gpt-oss-120b"]
    assert not residency._displaced, "an operator LOAD must not schedule its victim for restore"

    gw2 = FakeLocalGateway(running={"gpt-oss-120b"})
    residency2 = ResidencyCoordinator(
        gw2,
        ResidencyWiring.inert(enabled=True, free_ram_fraction=settings.local_llm_free_ram_fraction),
    )
    asyncio.run(_admit_or_409(residency2, "qwen3-coder-next", transient=True))
    assert gw2.unloaded == ["gpt-oss-120b"]
    assert residency2._displaced == {"gpt-oss-120b"}, "a PRIME must put back what it displaced"
