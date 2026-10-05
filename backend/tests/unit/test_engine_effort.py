"""Per-engine reasoning levels (jbrain.llm.engine_effort): the owner's Flash-Next level per task
and per tier, resolved task row → tier row → today's Standard effort, applied only to calls that
run on Flash-Next, with a per-call `effort_override` still winning. The router, the warm-up, the
cache, the snapshot and the owner/debug routes; the table's RLS is in
tests/integration/test_llm_engine_effort_rls.py."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from jbrain.api import llm_settings
from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.llm import FakeLlmClient, engine_effort
from jbrain.llm import engine as engines
from jbrain.llm.engine_effort import EngineEffortCache, EngineEfforts
from jbrain.llm.router import (
    LlmRouter,
    resolve_tasks,
    resolve_tiers,
    task_tier,
    warm_reasoning_effort,
)
from jbrain.llm.types import UserMessage
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeLocalGateway, FakeSettingsStore

FN = "qwen3.8-flash-next"
FLASH = engines.FLASH_NEXT
_DB = "postgresql+asyncpg://nobody@localhost:1/none"


# --- the levels and the resolution -----------------------------------------------------------


def test_the_model_default_is_the_template_default_in_our_terms() -> None:
    assert engine_effort.model_default(FLASH) == "high"
    assert engine_effort.model_default(engines.STANDARD) is None


def test_flash_next_takes_the_four_levels_and_standard_takes_none() -> None:
    # "none" is the hybrid's thinking-off toggle; the rest are its thinking_effort_map keys.
    assert engine_effort.levels_for(FLASH) == ("none", "low", "medium", "high")
    assert engine_effort.levels_for(engines.STANDARD) == ()


def test_a_task_row_beats_its_tier_row_which_beats_nothing() -> None:
    efforts = EngineEfforts({(FLASH, "tier", "medium"): "low"})
    assert efforts.resolve(FLASH, "agent.turn", "medium") == ("low", "tier")
    efforts = EngineEfforts(
        {(FLASH, "tier", "medium"): "low", (FLASH, "task", "agent.turn"): "high"}
    )
    assert efforts.resolve(FLASH, "agent.turn", "medium") == ("high", "task")
    assert efforts.resolve(FLASH, "wiki.rewrite", "medium") == ("low", "tier")
    assert efforts.resolve(FLASH, "fact.adjudicate", "high") == (None, None)
    assert efforts.resolve(FLASH, "pet.turn", None) == (None, None)
    # Rows are per engine.
    assert efforts.resolve(engines.STANDARD, "agent.turn", "medium") == (None, None)


def test_tasks_sit_in_the_screens_role_groups() -> None:
    assert task_tier("fact.adjudicate") == "high"
    assert task_tier("agent.turn") == "medium"
    assert task_tier("triage.classify") == "low"
    assert task_tier("vision.ocr") == "vision" and task_tier("agent.vision") == "vision"
    # The hidden title follows the chat MODEL but keeps its own low effort, so the low tier.
    assert task_tier("research.title") == "low"
    # Code mode's two roles are their own tier.
    assert task_tier("jcode.executor") == "code" and task_tier("jcode.planner") == "code"


# --- the router ------------------------------------------------------------------------------


class _Admit:
    async def ensure_room(self, served_model: str) -> None:
        return None


def _router(
    engine_now: dict[str, Any],
    rows: dict[tuple[str, str, str], str] | None = None,
    overrides: dict[str, dict[str, str]] | None = None,
    *,
    loader: Callable[[], Awaitable[EngineEfforts]] | None = None,
    reads: list[int] | None = None,
) -> tuple[LlmRouter, FakeLlmClient, FakeLlmClient]:
    local, cloud = FakeLlmClient(["local"]), FakeLlmClient(["cloud"])
    stored = overrides or {}

    async def _overrides() -> dict[str, dict[str, str]]:
        return stored

    async def _engine() -> engines.Engine:
        return engine_now["engine"]

    async def _efforts() -> EngineEfforts:
        if reads is not None:
            reads.append(1)
        return EngineEfforts(dict(rows or {}))

    router = LlmRouter(
        {"local": local, "xai": cloud, "anthropic": cloud},
        resolve_tasks({}),
        tiers=resolve_tiers({}),
        overrides_loader=_overrides,
        residency=_Admit(),
        engine_loader=_engine,
        engine_efforts_loader=loader or _efforts,
    )
    return router, local, cloud


_ALL_LOCAL = {
    "agent.turn": {"spec": "local:gpt-oss-120b", "reasoning_effort": "high"},
    "wiki.rewrite": {"spec": "local:gpt-oss-120b"},
    "fact.adjudicate": {"spec": "local:gpt-oss-120b"},
    "vision.ocr": {"spec": "local:qwen3-vl-30b-a3b"},
}


@pytest.mark.asyncio
async def test_on_flash_next_a_task_row_then_its_tier_then_the_standard_effort() -> None:
    rows = {(FLASH, "tier", "medium"): "low", (FLASH, "task", "wiki.rewrite"): "none"}
    router, local, _ = _router({"engine": FLASH}, rows, _ALL_LOCAL)

    for task in ("agent.turn", "wiki.rewrite", "fact.adjudicate"):
        await router.complete(task, system="s", user_text="u")
    efforts = [(c["model"], c["reasoning_effort"]) for c in local.calls]
    assert efforts == [
        (FN, "low"),  # the medium tier's level, over the stored Standard "high"
        (FN, "none"),  # its own row, over the tier
        (FN, "high"),  # no rows: today's behaviour, the bucket default
    ]


@pytest.mark.asyncio
async def test_a_vision_task_remapped_onto_flash_next_takes_the_vision_tier() -> None:
    router, local, _ = _router({"engine": FLASH}, {(FLASH, "tier", "vision"): "none"}, _ALL_LOCAL)
    await router.complete("vision.ocr", system="s", user_text="u")
    assert local.calls[-1]["model"] == FN and local.calls[-1]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_standard_never_reads_the_levels_and_routes_exactly_as_before() -> None:
    rows = {(FLASH, "tier", "medium"): "low", (FLASH, "task", "agent.turn"): "none"}
    reads: list[int] = []
    router, local, _ = _router({"engine": engines.STANDARD}, rows, _ALL_LOCAL, reads=reads)
    bare, bare_local, _ = _router({"engine": engines.STANDARD}, None, _ALL_LOCAL)
    bare._engine_efforts_loader = None

    for r in (router, bare):
        await r.complete("agent.turn", system="s", user_text="u")
        await r.complete("wiki.rewrite", system="s", user_text="u")
    assert local.calls == bare_local.calls
    assert [c["reasoning_effort"] for c in local.calls] == ["high", None]
    assert reads == []


@pytest.mark.asyncio
async def test_a_cloud_route_on_flash_next_keeps_its_own_effort() -> None:
    rows = {(FLASH, "task", "agent.turn"): "none"}
    router, local, cloud = _router({"engine": FLASH}, rows)
    await router.complete("agent.turn", system="s", user_text="u")
    assert local.calls == [] and cloud.calls[-1]["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_a_per_call_effort_override_still_wins_on_flash_next() -> None:
    rows = {(FLASH, "task", "agent.turn"): "none"}
    router, local, _ = _router({"engine": FLASH}, rows, _ALL_LOCAL)
    msgs = [UserMessage(text="hi")]

    await router.converse("agent.turn", system="s", messages=msgs)
    assert local.converse_calls[-1]["reasoning_effort"] == "none"
    await router.converse("agent.turn", system="s", messages=msgs, effort_override="high")
    assert local.converse_calls[-1]["reasoning_effort"] == "high"
    assert await router.effective_reasoning_effort("agent.turn") == "none"
    assert await router.effective_reasoning_effort("agent.turn", effort_override="low") == "low"


@pytest.mark.asyncio
async def test_a_per_call_local_spec_override_lands_on_flash_next_with_its_level() -> None:
    rows = {(FLASH, "tier", "medium"): "medium"}
    router, local, _ = _router({"engine": FLASH}, rows)
    await router.complete(
        "agent.turn", system="s", user_text="u", spec_override="local:qwen3.8-27b-q4"
    )
    assert local.calls[-1]["model"] == FN and local.calls[-1]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_standard_effort() -> None:
    async def _broken() -> EngineEfforts:
        raise RuntimeError("db down")

    router, local, _ = _router({"engine": FLASH}, overrides=_ALL_LOCAL, loader=_broken)
    await router.complete("agent.turn", system="s", user_text="u")
    assert local.calls[-1]["reasoning_effort"] == "high"


def test_the_warm_up_carries_the_engine_level() -> None:
    assert warm_reasoning_effort("agent.turn", FN, "high", "low") == "low"
    assert warm_reasoning_effort("agent.turn", FN, "high", None) == "high"
    # Still gated on the model being warmed.
    assert warm_reasoning_effort("agent.turn", "qwen3-vl-30b-a3b", None, "low") is None


@pytest.mark.asyncio
async def test_the_load_warm_identity_resolves_agent_turns_flash_next_level() -> None:
    store = FakeSettingsStore()
    store.values["llm_task_overrides"] = {"agent.turn": {"reasoning_effort": "high"}}
    store.engine_effort_rows[(FLASH, "tier", "medium")] = "low"
    effort, _, _ = await llm_settings._warm_identity(
        FN, settings_store=cast(Any, store), kv_prefix=None, registry=None
    )
    assert effort == "low"
    # A Standard model's warm never sees the Flash-Next rows.
    effort, _, _ = await llm_settings._warm_identity(
        "gpt-oss-120b", settings_store=cast(Any, store), kv_prefix=None, registry=None
    )
    assert effort == "high"


# --- the cache -------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_the_cache_holds_for_its_ttl_and_a_write_invalidates_it() -> None:
    store = FakeSettingsStore()
    clock = _Clock()
    reads: list[int] = []

    async def _load() -> EngineEfforts:
        reads.append(1)
        return await store.llm_engine_efforts(None)

    cache = EngineEffortCache(_load, ttl_s=5.0, clock=clock)
    assert (await cache.get()).rows == {}
    await store.set_llm_engine_efforts(None, {(FLASH, "task", "agent.turn"): "low"})
    # The write invalidated every in-process cache: seen at once, not after the TTL.
    assert (await cache.get()).level(FLASH, "task", "agent.turn") == "low"
    await cache.get()
    assert len(reads) == 2
    store.engine_effort_rows[(FLASH, "task", "agent.turn")] = "high"  # another process wrote
    assert (await cache.get()).level(FLASH, "task", "agent.turn") == "low"
    clock.now = 5.0
    assert (await cache.get()).level(FLASH, "task", "agent.turn") == "high"


@pytest.mark.asyncio
async def test_a_read_racing_a_write_is_not_kept_as_fresh() -> None:
    """A load that began before an invalidate saw the rows before the write; storing it would
    hide the owner's change for a whole TTL."""
    rows: dict[tuple[str, str, str], str] = {}
    entered, release = asyncio.Event(), asyncio.Event()
    reads: list[int] = []

    async def _load() -> EngineEfforts:
        snapshot = dict(rows)
        reads.append(1)
        if len(reads) == 1:
            entered.set()
            await release.wait()
        return EngineEfforts(snapshot)

    cache = EngineEffortCache(_load, ttl_s=60.0, clock=_Clock())
    slow = asyncio.create_task(cache.get())
    await entered.wait()
    rows[(FLASH, "task", "agent.turn")] = "low"  # the owner writes while the read is out
    cache.invalidate()
    release.set()
    assert (await slow).rows == {}  # that call answers with what it read...
    # ...but the next one reads again rather than trusting the stale value for the TTL.
    assert (await cache.get()).level(FLASH, "task", "agent.turn") == "low"
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_the_cache_keeps_the_last_value_when_a_read_fails() -> None:
    clock = _Clock()
    value: dict[str, Any] = {"rows": {(FLASH, "tier", "low"): "none"}}

    async def _load() -> EngineEfforts:
        if value["rows"] is None:
            raise RuntimeError("db down")
        return EngineEfforts(value["rows"])

    cache = EngineEffortCache(_load, ttl_s=1.0, clock=clock)
    assert (await cache.get()).level(FLASH, "tier", "low") == "none"
    value["rows"] = None
    clock.now = 2.0
    assert (await cache.get()).level(FLASH, "tier", "low") == "none"


# --- the API ---------------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path: Path) -> Iterator[tuple[TestClient, FakeSettingsStore, str]]:
    settings = Settings(
        secure_cookies=False,
        database_url=_DB,
        xai_api_key="test-xai",
        anthropic_api_key="test-anthropic",
        debug_access_enabled=True,
        local_models_dir=str(tmp_path),
        local_llm_enabled=True,
        local_models=["gpt-oss-120b", FN],
    )
    app = create_app(settings)
    repo = FakeAuthRepo()
    store = FakeSettingsStore()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        app.state.settings_store = store
        app.state.local_gateway = FakeLocalGateway()
        key = asyncio.run(auth_service.rotate_owner_key(repo))
        resp = client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        assert resp.status_code == 204
        debug_key, _ = asyncio.run(auth_service.mint_capability(repo, "claude", ttl_hours=24))
        yield client, store, debug_key


_BASE = "/api/settings/llm/engine-effort/flash-next"


def _flash(body: dict[str, Any]) -> dict[str, Any]:
    return body["engine_efforts"]["flash-next"]


def test_the_snapshot_reports_levels_tiers_and_tasks(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    client, store, _ = box
    store.values["llm_task_overrides"] = {
        "agent.turn": {"spec": "local:gpt-oss-120b", "reasoning_effort": "low"}
    }
    flash = _flash(client.get("/api/settings/llm").json())
    assert set(client.get("/api/settings/llm").json()["engine_efforts"]) == {"flash-next"}
    assert flash["label"] == "Flash-Next" and flash["active"] is False
    assert flash["levels"] == ["none", "low", "medium", "high"]
    # Unset sends no level, and Qwen3.8's template then thinks at xhigh — our "high".
    assert flash["model_default"] == "high"
    assert {t["id"]: (t["level"], t["default"]) for t in flash["tiers"]} == {
        "high": (None, "high"),
        "medium": (None, None),  # medium is sent as no level: the model's own default
        "low": (None, "low"),
        "vision": (None, None),
        "code": (None, None),  # grok's own level, or the model's, absent a row
    }
    assert flash["tiers"][-1]["label"] == "Code mode"
    tasks = {t["id"]: t for t in flash["tasks"]}
    assert "research.title" not in tasks  # hidden, follows the chat model
    assert tasks["agent.turn"] == {
        "id": "agent.turn",
        "tier": "medium",
        "label": None,  # named by its pick on the screen
        "level": None,
        "fallback": "low",  # its stored Standard effort
        "fallback_source": "standard",
        "effective": "low",
        "applies": False,  # Standard serves
    }
    assert tasks["fact.adjudicate"]["fallback"] == "high"

    store.values["llm_local_engine_effective"] = "flash-next"
    flash = _flash(client.get("/api/settings/llm").json())
    tasks = {t["id"]: t for t in flash["tasks"]}
    assert flash["active"] is True
    assert tasks["agent.turn"]["applies"] is True  # local, remapped onto Flash-Next
    assert tasks["fact.adjudicate"]["applies"] is False  # grok: never reads these levels


def test_set_a_tier_then_a_task_then_clear_them(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    client, store, _ = box
    resp = client.put(f"{_BASE}/tier/medium", json={"effort": "low"})
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in _flash(resp.json())["tasks"]}
    assert tasks["agent.turn"]["fallback"] == "low"
    assert tasks["agent.turn"]["fallback_source"] == "tier"
    assert tasks["agent.turn"]["effective"] == "low"

    resp = client.put(f"{_BASE}/task/agent.turn", json={"effort": "high"})
    tasks = {t["id"]: t for t in _flash(resp.json())["tasks"]}
    assert (tasks["agent.turn"]["level"], tasks["agent.turn"]["effective"]) == ("high", "high")
    assert tasks["wiki.rewrite"]["effective"] == "low"  # still inherits the tier
    assert store.engine_effort_rows == {
        (FLASH, "tier", "medium"): "low",
        (FLASH, "task", "agent.turn"): "high",
    }

    resp = client.delete(f"{_BASE}/task/agent.turn")
    tasks = {t["id"]: t for t in _flash(resp.json())["tasks"]}
    assert tasks["agent.turn"]["level"] is None and tasks["agent.turn"]["effective"] == "low"
    resp = client.delete(f"{_BASE}/tier/medium")
    assert resp.status_code == 200 and store.engine_effort_rows == {}
    # Clearing what is not set is a no-op, not a 404.
    assert client.delete(f"{_BASE}/tier/medium").status_code == 200
    # The Standard picks were never touched.
    assert "llm_task_overrides" not in store.values


def test_the_batch_sets_and_clears_together(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    client, store, _ = box
    store.engine_effort_rows[(FLASH, "task", "wiki.rewrite")] = "high"
    resp = client.put(
        _BASE,
        json={"tiers": {"medium": "medium", "vision": "none"}, "tasks": {"wiki.rewrite": None}},
    )
    assert resp.status_code == 200
    assert store.engine_effort_rows == {
        (FLASH, "tier", "medium"): "medium",
        (FLASH, "tier", "vision"): "none",
    }


def test_the_browse_step_thinks_low_and_its_level_is_the_owners_to_set(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    """It follows the chat MODEL, so it has no picker row — but its reasoning level is its
    own: listed by name, low by default, and settable like any task's."""
    client, store, _ = box
    tasks = {t["id"]: t for t in _flash(client.get("/api/settings/llm").json())["tasks"]}
    step = tasks["browse.step"]
    assert (step["tier"], step["label"], step["fallback"]) == ("low", "Browser agent step", "low")
    assert tasks["agent.turn"]["label"] is None  # a picker row still names its own task

    resp = client.put(f"{_BASE}/task/browse.step", json={"effort": "medium"})
    assert resp.status_code == 200
    assert store.engine_effort_rows == {(FLASH, "task", "browse.step"): "medium"}


@pytest.mark.asyncio
async def test_a_browse_step_on_flash_next_sends_low_unless_the_owner_set_a_level() -> None:
    # It follows agent.turn onto the local engine; medium would have sent no level at all,
    # which Flash-Next's template reads as xhigh.
    for rows, expected in (({}, "low"), ({(FLASH, "task", "browse.step"): "none"}, "none")):
        router, local, _ = _router({"engine": FLASH}, rows, _ALL_LOCAL)
        await router.converse("browse.step", system="s", messages=[UserMessage(text="u")])
        assert (local.converse_calls[0]["model"], local.converse_calls[0]["reasoning_effort"]) == (
            FN,
            expected,
        )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", f"{_BASE}/task/agent.turn", {"effort": "xhigh"}),  # not a Flash-Next level
        ("PUT", f"{_BASE}/task/agent.turn", {"effort": "low", "x": 1}),
        ("PUT", f"{_BASE}/task/nope.task", {"effort": "low"}),
        ("PUT", f"{_BASE}/task/research.title", {"effort": "low"}),  # hidden
        ("PUT", f"{_BASE}/tier/ultra", {"effort": "low"}),
        ("PUT", f"{_BASE}/model/agent.turn", {"effort": "low"}),  # unknown scope
        ("DELETE", f"{_BASE}/tier/ultra", None),
        ("PUT", "/api/settings/llm/engine-effort/standard/tier/medium", {"effort": "low"}),
        ("PUT", "/api/settings/llm/engine-effort/turbo/tier/medium", {"effort": "low"}),
        ("PUT", _BASE, {"tiers": {"medium": "low"}, "tasks": {"agent.turn": "max"}}),
        ("PUT", _BASE, {"roles": {}}),
    ],
)
def test_invalid_changes_are_422_and_write_nothing(
    box: tuple[TestClient, FakeSettingsStore, str], method: str, path: str, body: Any
) -> None:
    client, store, _ = box
    assert client.request(method, path, json=body).status_code == 422
    assert store.engine_effort_rows == {}


def test_the_routes_need_a_session() -> None:
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    with TestClient(app) as anon:
        assert anon.put(f"{_BASE}/tier/medium", json={"effort": "low"}).status_code == 401
        assert anon.delete(f"{_BASE}/tier/medium").status_code == 401
        assert anon.put(_BASE, json={}).status_code == 401


@pytest.mark.parametrize("kind", ["device_key", "jcode_share_link", "capability_token"])
def test_a_non_owner_session_is_refused_and_writes_nothing(
    box: tuple[TestClient, FakeSettingsStore, str], kind: str
) -> None:
    client, store, _ = box
    app: Any = client.app
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="other", kind=kind, label="not the owner", jcode_session_id="s1"
    )
    try:
        assert client.put(f"{_BASE}/tier/medium", json={"effort": "low"}).status_code == 403
        assert client.delete(f"{_BASE}/tier/medium").status_code == 403
        assert client.put(_BASE, json={"tiers": {"medium": "low"}}).status_code == 403
    finally:
        app.dependency_overrides.pop(current_principal, None)
    assert store.engine_effort_rows == {}


def test_the_debug_twin_shares_the_validation_and_the_write(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    client, store, key = box
    url = "/api/debug/llm/engine-effort/flash-next"
    headers = {"Authorization": f"Bearer {key}"}
    assert client.put(url, json={"tiers": {"low": "none"}}).status_code == 401
    resp = client.put(url, json={"tiers": {"low": "none"}}, headers=headers)
    assert resp.status_code == 200
    assert _flash(resp.json())["tiers"][2] == {
        "id": "low",
        "label": "Low reasoning",
        "level": "none",
        "default": "low",
    }
    assert client.put(url, json={"tiers": {"low": "lots"}}, headers=headers).status_code == 422
    assert store.engine_effort_rows == {(FLASH, "tier", "low"): "none"}


def test_code_modes_roles_are_a_code_tier_on_the_snapshot(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    client, store, _ = box
    app: Any = client.app
    flash = _flash(client.get("/api/settings/llm").json())
    code = [t for t in flash["tasks"] if t["tier"] == "code"]
    # Last, executor first, each with the name the screen shows (no pick to read one from).
    assert flash["tasks"][-2:] == code
    assert code == [
        {
            "id": "jcode.executor",
            "tier": "code",
            "label": "Code mode — executor",
            "level": None,
            "fallback": None,
            "fallback_source": "standard",
            "effective": None,
            "applies": False,
        },
        {
            "id": "jcode.planner",
            "tier": "code",
            "label": "Code mode — planner",
            "level": None,
            "fallback": None,
            "fallback_source": "standard",
            "effective": None,
            "applies": False,
        },
    ]
    # Applies only while Flash-Next serves AND code mode is on.
    store.values["llm_local_engine_effective"] = "flash-next"
    code = {t["id"]: t for t in _flash(client.get("/api/settings/llm").json())["tasks"]}
    assert code["jcode.executor"]["applies"] is False
    app.state.settings.jcode_enabled = True
    code = {t["id"]: t for t in _flash(client.get("/api/settings/llm").json())["tasks"]}
    assert code["jcode.executor"]["applies"] is True and code["jcode.planner"]["applies"] is True


def test_code_mode_levels_set_by_tier_and_role(
    box: tuple[TestClient, FakeSettingsStore, str],
) -> None:
    client, store, _ = box
    resp = client.put(f"{_BASE}/tier/code", json={"effort": "low"})
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in _flash(resp.json())["tasks"]}
    assert tasks["jcode.planner"]["fallback"] == "low"
    assert tasks["jcode.planner"]["fallback_source"] == "tier"
    resp = client.put(f"{_BASE}/task/jcode.planner", json={"effort": "high"})
    tasks = {t["id"]: t for t in _flash(resp.json())["tasks"]}
    planner = tasks["jcode.planner"]
    assert (planner["level"], planner["effective"]) == ("high", "high")
    assert tasks["jcode.executor"]["effective"] == "low"
    resp = client.put(_BASE, json={"tasks": {"jcode.executor": "none", "jcode.planner": None}})
    assert resp.status_code == 200
    assert store.engine_effort_rows == {
        (FLASH, "tier", "code"): "low",
        (FLASH, "task", "jcode.executor"): "none",
    }
    # A level Flash-Next does not take, or a role that does not exist, is still a 422.
    assert client.put(f"{_BASE}/task/jcode.executor", json={"effort": "xhigh"}).status_code == 422
    assert client.put(f"{_BASE}/task/jcode.reviewer", json={"effort": "low"}).status_code == 422
