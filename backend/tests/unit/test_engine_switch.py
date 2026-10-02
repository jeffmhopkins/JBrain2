"""Flash-Next F3a (docs/plans/FLASH_NEXT_ENGINE_PLAN.md): the owner engine switch and its ONE
orchestration (jbrain.llm.engine_switch), the cross-process drain (jbrain.llm.drain), the
nightly guard, and the remap — every `local:*` route onto Flash-Next while it serves, and a
Flash-Next pick back to the task default while Standard serves — at every entry point. The
supervisor, the gateway and the providers are faked; nothing touches docker or a model."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain import box_events
from jbrain.api import engine as engine_api
from jbrain.api import jcode_llm, llm_settings
from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.llm import FakeLlmClient, drain, local_catalog, providers
from jbrain.llm import engine as engines
from jbrain.llm.engine_switch import (
    EngineSwitcher,
    HoldRefused,
    SupervisorError,
    SwitchDeps,
    SwitchRefused,
    Timings,
    reset_after_restart,
    smoke_model,
)
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError, ResidencyWiring
from jbrain.llm.router import (
    LlmRouter,
    context_window_for_spec,
    resolve_tasks,
    resolve_tiers,
    spec_on_engine,
    warm_reasoning_effort,
)
from jbrain.main import create_app
from jbrain.workflow.scheduler import ScheduleWindow, nightly_window_reason
from tests.unit.fakes import FakeAuthRepo, FakeLocalGateway, FakeSettingsStore
from tests.unit.test_debug_flash_next import _Gateway, _Supervisor

FN = "qwen3.8-flash-next"
_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_FAST = Timings(
    drain_s=0.0, settle_s=0.0, memory_settle_s=0.0, poll_s=0.0, gate_wait_s=0.0, engine_cache_s=0.0
)
_INSTALLED = ["gpt-oss-120b", "qwen3.5-4b", FN]
_TERMINAL = {"done", "rolled_back", "failed"}


def _engine_loader(value: dict[str, Any]) -> Callable[[], Awaitable[engines.Engine]]:
    async def _load() -> engines.Engine:
        return value["engine"]

    return _load


# --- the remap: catalog + module-level helpers ----------------------------------------------


def test_remap_for_engine_both_directions() -> None:
    assert local_catalog.remap_for_engine("gpt-oss-120b", engines.FLASH_NEXT) == FN
    assert local_catalog.remap_for_engine("an-operator-model", engines.FLASH_NEXT) == FN
    assert local_catalog.remap_for_engine(FN, engines.FLASH_NEXT) == FN
    assert local_catalog.remap_for_engine("gpt-oss-120b", engines.STANDARD) == "gpt-oss-120b"
    # Standard has a roster, not a sole model: a Flash-Next pick has nothing to map onto.
    assert local_catalog.remap_for_engine(FN, engines.STANDARD) is None
    assert local_catalog.sole_model(engines.STANDARD) is None


def test_spec_on_engine_and_the_module_level_capabilities() -> None:
    gpt = "local:gpt-oss-120b"
    assert spec_on_engine(gpt, engines.FLASH_NEXT, "xai:grok-4.3") == f"local:{FN}"
    assert spec_on_engine(f"local:{FN}", engines.STANDARD, "xai:grok-4.3") == "xai:grok-4.3"
    assert spec_on_engine("xai:grok-4.3", engines.FLASH_NEXT, "x:y") == "xai:grok-4.3"
    # Engine passed explicitly: the window and vision are those of the model that RUNS.
    assert context_window_for_spec(gpt, engines.FLASH_NEXT) == 262144
    assert context_window_for_spec(gpt, engines.STANDARD) == local_catalog.context_window(
        "gpt-oss-120b"
    )
    settings = Settings(
        database_url=_DB, local_llm_enabled=True, local_models=_INSTALLED, xai_api_key=""
    )
    assert providers.supports_vision_for_spec(settings, gpt, engines.STANDARD) is False
    assert providers.supports_vision_for_spec(settings, gpt, engines.FLASH_NEXT) is True


def test_a_stored_flash_next_pick_reverse_maps_whichever_engine_serves() -> None:
    settings = Settings(
        database_url=_DB, local_llm_enabled=True, local_models=_INSTALLED, xai_api_key=""
    )
    assert providers.id_for_spec(settings, f"local:{FN}") == FN
    assert providers.supports_reasoning(settings, FN) is True


def test_warm_effort_follows_the_model_actually_loaded() -> None:
    assert warm_reasoning_effort("agent.turn", FN, "high") == "high"
    assert warm_reasoning_effort("agent.turn", "qwen3-vl-30b-a3b", "high") is None


# --- the remap at the router ---------------------------------------------------------------


class _Admit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_room(self, served_model: str) -> None:
        self.calls.append(served_model)


def _router(
    engine_now: dict[str, Any],
    overrides: dict[str, dict[str, str]] | None = None,
    *,
    tasks: dict[str, str] | None = None,
    tiers: dict[str, str] | None = None,
    gate: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[LlmRouter, FakeLlmClient, FakeLlmClient, _Admit]:
    local, cloud = FakeLlmClient(["local"]), FakeLlmClient(["cloud"])
    admit = _Admit()
    stored = overrides or {}

    async def _overrides() -> dict[str, dict[str, str]]:
        return stored

    router = LlmRouter(
        {"local": local, "xai": cloud, "anthropic": cloud},
        resolve_tasks(tasks or {}),
        tiers=resolve_tiers(tiers or {}),
        pinned=frozenset(tasks or {}),
        overrides_loader=_overrides,
        residency=admit,
        engine_loader=_engine_loader(engine_now),
        admission_gate=gate,
    )
    return router, local, cloud, admit


@pytest.mark.asyncio
async def test_every_local_pick_runs_on_flash_next_with_its_own_sampling_and_effort() -> None:
    engine_now = {"engine": engines.FLASH_NEXT}
    picks = {"agent.turn": {"spec": "local:gpt-oss-120b", "reasoning_effort": "high"}}
    router, local, _, admit = _router(engine_now, picks)

    await router.complete("agent.turn", system="s", user_text="u")

    call = local.calls[-1]
    assert call["model"] == FN and admit.calls == [FN]
    assert call["reasoning_effort"] == "high"
    flash = local_catalog.get(FN)
    assert flash is not None and call["sampling"] == flash.sampling_thinking
    # The stored pick is untouched.
    assert picks["agent.turn"]["spec"] == "local:gpt-oss-120b"


@pytest.mark.asyncio
async def test_a_non_reasoning_pick_gains_the_task_effort_on_flash_next() -> None:
    engine_now = {"engine": engines.FLASH_NEXT}
    router, local, _, _ = _router(engine_now, {"fact.adjudicate": {"spec": "local:qwen3-30b-a3b"}})
    await router.complete("fact.adjudicate", system="s", user_text="u")
    assert local.calls[-1]["model"] == FN and local.calls[-1]["reasoning_effort"] == "high"
    engine_now["engine"] = engines.STANDARD
    await router.complete("fact.adjudicate", system="s", user_text="u")
    assert local.calls[-1]["model"] == "qwen3-30b-a3b"
    assert local.calls[-1]["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_tiers_env_pins_and_per_call_overrides_are_remapped() -> None:
    engine_now = {"engine": engines.FLASH_NEXT}
    router, local, _, _ = _router(
        engine_now,
        tasks={"wiki.ground": "local:qwen3.8-27b"},
        tiers={"high": "local:gpt-oss-120b"},
    )
    await router.complete("wiki.ground", system="s", user_text="u")
    await router.complete("triage.classify", system="s", user_text="u", strength="high")
    await router.complete(
        "agent.turn", system="s", user_text="u", spec_override="local:qwen3.8-27b-q4"
    )
    assert [c["model"] for c in local.calls] == [FN, FN, FN]


@pytest.mark.asyncio
async def test_cloud_routes_are_never_remapped() -> None:
    router, local, cloud, admit = _router({"engine": engines.FLASH_NEXT})
    await router.complete("agent.turn", system="s", user_text="u")
    assert cloud.calls[-1]["model"] == "grok-4.3" and local.calls == [] and admit.calls == []


@pytest.mark.asyncio
async def test_a_flash_next_pick_falls_back_to_the_task_default_while_standard_serves() -> None:
    engine_now = {"engine": engines.STANDARD}
    router, local, cloud, _ = _router(engine_now, {"agent.turn": {"spec": f"local:{FN}"}})
    await router.complete("agent.turn", system="s", user_text="u")
    assert cloud.calls[-1]["model"] == "grok-4.3" and local.calls == []
    # With a local static route, the fallback is that model, never a refusal.
    router, local, _, _ = _router(
        engine_now,
        {"agent.turn": {"spec": f"local:{FN}"}},
        tasks={"agent.turn": "local:gpt-oss-120b"},
    )
    await router.complete("agent.turn", system="s", user_text="u")
    assert local.calls[-1]["model"] == "gpt-oss-120b"
    # Static route also Flash-Next: left for residency to refuse with the sentence.
    router, local, _, admit = _router(engine_now, tasks={"agent.turn": f"local:{FN}"})
    await router.complete("agent.turn", system="s", user_text="u")
    assert admit.calls == [FN]


@pytest.mark.asyncio
async def test_the_warm_keepers_target_and_the_followed_title_move_to_flash_next() -> None:
    engine_now = {"engine": engines.FLASH_NEXT}
    router, local, _, _ = _router(engine_now, {"agent.turn": {"spec": "local:gpt-oss-120b"}})
    assert await router.primary_local_served_model() == FN
    await router.complete("research.title", system="s", user_text="u")
    assert local.calls[-1]["model"] == FN
    engine_now["engine"] = engines.STANDARD
    assert await router.primary_local_served_model() == "gpt-oss-120b"


@pytest.mark.asyncio
async def test_window_vision_and_effective_spec_follow_the_remap() -> None:
    engine_now = {"engine": engines.FLASH_NEXT}
    router, _, _, _ = _router(engine_now, {"agent.turn": {"spec": "local:gpt-oss-120b"}})
    assert await router.context_window("agent.turn") == 262144
    assert await router.supports_vision("agent.turn") is True
    assert await router.effective_spec("agent.turn") == ("local", FN)


@pytest.mark.asyncio
async def test_a_local_call_held_at_the_gate_resolves_again_after_the_switch() -> None:
    engine_now = {"engine": engines.STANDARD}

    async def gate() -> bool:
        engine_now["engine"] = engines.FLASH_NEXT  # the switch lands while the call waits
        return True

    router, local, _, _ = _router(
        engine_now, {"agent.turn": {"spec": "local:gpt-oss-120b"}}, gate=gate
    )
    turn = await router.converse("agent.turn", system="s", messages=[])
    assert turn is not None and local.converse_calls[-1]["model"] == FN


@pytest.mark.asyncio
async def test_a_closed_gate_refuses_local_calls_and_never_touches_cloud_ones() -> None:
    async def closed() -> bool:
        raise drain.LocalAdmissionClosedError("switching")

    engine_now = {"engine": engines.STANDARD}
    router, local, cloud, _ = _router(
        engine_now, {"agent.turn": {"spec": "local:gpt-oss-120b"}}, gate=closed
    )
    with pytest.raises(ResidencyError):
        await router.complete("agent.turn", system="s", user_text="u")
    assert local.calls == []
    await router.complete("fact.adjudicate", system="s", user_text="u")
    assert cloud.calls


# --- residency -----------------------------------------------------------------------------


def _coord(
    engine_now: dict[str, Any],
    gw: FakeLocalGateway,
    gate: Callable[[], Awaitable[bool]] | None = None,
) -> ResidencyCoordinator:
    return ResidencyCoordinator(
        gw,
        ResidencyWiring.inert(
            enabled=True, engine_loader=_engine_loader(engine_now), admission_gate=gate
        ),
    )


@pytest.mark.asyncio
async def test_residency_never_evicts_flash_next_for_an_old_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    gw = FakeLocalGateway(running={FN})
    await _coord({"engine": engines.FLASH_NEXT}, gw).ensure_room("gpt-oss-120b")
    assert gw.unloaded == [] and gw.loaded == []


@pytest.mark.asyncio
async def test_residency_waits_at_the_gate_before_any_eviction() -> None:
    async def closed() -> bool:
        raise drain.LocalAdmissionClosedError("switching")

    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord({"engine": engines.STANDARD}, gw, closed)
    with pytest.raises(drain.LocalAdmissionClosedError):
        await coord.ensure_room("qwen3.5-4b")
    with pytest.raises(drain.LocalAdmissionClosedError):
        await coord.free_room("qwen3.5-4b")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_restore_puts_flash_next_back_in_place_of_a_displaced_standard_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 0.0)
    )
    coord = _coord({"engine": engines.FLASH_NEXT}, FakeLocalGateway())
    coord._displaced.add("gpt-oss-120b")
    plan = await coord._restore_plan({"gpt-oss-120b"}, 0.15, {}, {})
    assert plan == [FN] and coord._displaced == {FN}


@pytest.mark.asyncio
async def test_a_restore_during_a_drain_is_skipped_and_rearmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def closed() -> bool:
        raise drain.LocalAdmissionClosedError("switching")

    gw = FakeLocalGateway()
    coord = _coord({"engine": engines.STANDARD}, gw, closed)
    coord._displaced.add("gpt-oss-120b")
    rearmed: list[bool] = []
    monkeypatch.setattr(coord, "_rearm_restore", lambda: rearmed.append(True))
    await coord._restore()
    assert rearmed == [True] and gw.loaded == []


# --- the admission gate --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_is_open_by_default_and_on_a_bad_or_expired_row() -> None:
    now = time.time()
    for row in (None, {}, {"closed": "yes"}, {"closed": True, "until": now - 1}, {"closed": True}):
        assert drain.closure_from(row, now) is None
    closure = drain.closure_from({"closed": True, "until": now + 60, "reason": "r"}, now)
    assert closure is not None and closure.reason == "r"

    async def broken() -> object:
        raise RuntimeError("db down")

    assert await drain.AdmissionGate(broken).wait_open() is False


@pytest.mark.asyncio
async def test_the_gate_crosses_processes_waits_and_reopens() -> None:
    store = FakeSettingsStore()
    # Two processes: the api's gate and the worker's, over the same row.
    api = drain.AdmissionGate(lambda: store.llm_local_admission(None), ttl_s=60.0)
    clock = {"t": 0.0}
    sleeps: list[float] = []

    async def _sleep(s: float) -> None:
        sleeps.append(s)
        clock["t"] += s
        if len(sleeps) == 3:
            await store.set_llm_local_admission(None, dict(drain.OPEN_ROW))

    worker = drain.AdmissionGate(
        lambda: store.llm_local_admission(None),
        ttl_s=0.0,
        wait_s=30.0,
        poll_s=1.0,
        clock=lambda: clock["t"],
        sleep=_sleep,
    )
    assert await api.wait_open() is False  # caches "open" for a minute
    await store.set_llm_local_admission(
        None, drain.closed_row("switching to flash-next", ttl_s=600, now=time.time())
    )
    drain.invalidate_cached()  # the writing process expires its own cache at once
    assert (await api.closure()) is not None
    assert await worker.wait_open() is True and len(sleeps) == 3


@pytest.mark.asyncio
async def test_the_gate_refuses_after_its_wait() -> None:
    store = FakeSettingsStore()
    await store.set_llm_local_admission(
        None, drain.closed_row("switching to standard", ttl_s=600, now=time.time())
    )
    clock = {"t": 0.0}

    async def _sleep(s: float) -> None:
        clock["t"] += s

    gate = drain.AdmissionGate(
        lambda: store.llm_local_admission(None),
        ttl_s=0.0,
        wait_s=3.0,
        clock=lambda: clock["t"],
        sleep=_sleep,
    )
    with pytest.raises(drain.LocalAdmissionClosedError, match="switching to standard"):
        await gate.wait_open()


# --- the orchestration (EngineSwitcher), against fakes --------------------------------------


class _Sup:
    def __init__(self, states: dict[str, str]) -> None:
        self.state = dict(states)
        self.events: list[str] = []
        self.busy: str | None = None
        self.start_404: set[str] = set()
        self.unreachable = False
        # Services whose stop is accepted but never takes; whose stop/start raises.
        self.stuck: set[str] = set()
        self.stop_error: set[str] = set()
        self.start_error: set[str] = set()
        # Services that crash on start (accepted, never reported running).
        self.start_crash: set[str] = set()
        self.holds: list[tuple[str, float]] = []
        self.released: list[str] = []
        self.hold_answer: str = "ok"  # "ok" | "refused" | "missing" | "error"
        self.start_ids: list[str | None] = []
        # A one-shot that appears once the preflight has read "none".
        self.busy_after_preflight: str | None = None

    async def hold(self, switch_id: str, ttl_s: float) -> bool:
        self.holds.append((switch_id, ttl_s))
        if self.hold_answer == "refused":
            raise HoldRefused("another engine switch holds it")
        if self.hold_answer == "error":
            raise SupervisorError("HTTP 500")
        if self.busy_after_preflight is not None:
            self.busy = self.busy_after_preflight
        return self.hold_answer == "ok"

    async def release(self, switch_id: str) -> None:
        self.released.append(switch_id)

    async def states(self) -> dict[str, str]:
        if self.unreachable:
            raise SupervisorError("no route")
        return dict(self.state)

    async def oneshot(self) -> str | None:
        if self.unreachable:
            raise SupervisorError("no route")
        return self.busy

    async def toggle(self, action: str, service: str, switch_id: str | None = None) -> int:
        self.events.append(f"{action} {service}")
        if action == "start":
            self.start_ids.append(switch_id)
            if service in self.start_error:
                raise SupervisorError("HTTP 500")
            if service in self.start_404:
                return 404
            self.state[service] = "exited" if service in self.start_crash else "running"
        else:
            if service in self.stop_error:
                raise SupervisorError("HTTP 500")
            if service not in self.stuck:
                self.state[service] = "exited"
        return 202


def _deps(
    sup: _Sup,
    gw: FakeLocalGateway | None,
    store: FakeSettingsStore,
    **kw: Any,
) -> SwitchDeps:
    return SwitchDeps(
        supervisor=sup,
        gateway=gw,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        ctx=None,
        local_models=kw.pop("local_models", _INSTALLED),
        **kw,
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None, str]]:
    events: list[tuple[str, str, str | None, str]] = []

    async def _record(kind: str, subject: str, *, detail: str | None = None, status: str = "ok"):
        events.append((kind, subject, detail, status))

    monkeypatch.setattr(box_events, "record", _record)
    return events


async def _run(sw: EngineSwitcher, deps: SwitchDeps, target: engines.Engine, **kw: Any) -> dict:
    await sw.begin(deps, target, source="owner", **kw)
    await sw.wait()
    status = await sw.status(deps)
    assert status is not None
    return status


@pytest.mark.asyncio
async def test_happy_path_switches_and_writes_a_box_event(recorded: list) -> None:
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    store = FakeSettingsStore()
    status = await _run(EngineSwitcher(_FAST), _deps(sup, gw, store), engines.FLASH_NEXT)

    assert status["stage"] == "done" and status["reason"] is None
    assert sup.events == ["stop local-llm", "start flash-next"]
    assert gw.unloaded == ["gpt-oss-120b"] and gw.loaded == [FN]
    assert gw.probed == [f"text {FN}", f"tool {FN}", f"image {FN}"]
    assert store.values["llm_local_engine"] == "flash-next"
    assert store.values["llm_local_engine_effective"] == "flash-next"
    assert store.values["llm_local_admission"] == drain.OPEN_ROW
    assert status == store.values["llm_local_engine_switch"]
    assert recorded == [
        (box_events.ENGINE_SWITCH, "flash-next", "switched from standard to flash-next", "ok")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["text", "tool", "image"])
async def test_a_failed_smoke_probe_rolls_back(recorded: list, probe: str) -> None:
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    gw.fail_probes = {probe}  # type: ignore[attr-defined]
    store = FakeSettingsStore()
    status = await _run(EngineSwitcher(_FAST), _deps(sup, gw, store), engines.FLASH_NEXT)

    assert status["stage"] == "rolled_back"
    assert f"smoke test failed: {probe} probe" in status["reason"]
    assert "standard was put back" in status["reason"]
    assert sup.events[-2:] == ["stop flash-next", "start local-llm"]
    assert FN in gw.unloaded  # released through the client before the stop
    assert "llm_local_engine" not in store.values
    assert store.values["llm_local_engine_effective"] == "standard"
    assert store.values["llm_local_admission"] == drain.OPEN_ROW
    assert recorded[-1][3] == "failed" and "rolled back" in str(recorded[-1][2])
    assert {s["probe"]: s["ok"] for s in status["smoke"]}[probe] is False


@pytest.mark.asyncio
async def test_a_failed_load_rolls_back() -> None:
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    gw = FakeLocalGateway(running={"gpt-oss-120b"}, fail_load=True)
    store = FakeSettingsStore()
    status = await _run(EngineSwitcher(_FAST), _deps(sup, gw, store), engines.FLASH_NEXT)
    assert status["stage"] == "rolled_back" and "did not load" in status["reason"]
    assert sup.state["local-llm"] == "running" and sup.state["flash-next"] == "exited"


@pytest.mark.asyncio
async def test_the_drain_waits_for_in_flight_calls_and_holds_admission_closed() -> None:
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    store = FakeSettingsStore()
    seen: list[object] = []

    class _Busy(FakeLocalGateway):
        polls = 0

        async def slots(self, served_model: str) -> list[dict[str, object]]:
            # What every process's next local call would read while the drain runs.
            seen.append(await store.llm_local_admission(None))
            self.polls += 1
            return [{"id": 0, "is_processing": self.polls < 3}]

    gw = _Busy(running={"gpt-oss-120b"})
    timings = Timings(
        drain_s=60, settle_s=0, memory_settle_s=0, poll_s=0, gate_wait_s=0, engine_cache_s=0
    )
    status = await _run(EngineSwitcher(timings), _deps(sup, gw, store), engines.FLASH_NEXT)

    assert status["stage"] == "done" and gw.polls == 3
    assert all(drain.closure_from(row, time.time()) is not None for row in seen)
    assert not any("drain timed out" in n for n in status["notes"])
    assert drain.closure_from(store.values["llm_local_admission"], time.time()) is None


@pytest.mark.asyncio
async def test_the_drain_is_bounded_then_proceeds() -> None:
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    gw.busy_slots = {"gpt-oss-120b"}  # type: ignore[attr-defined]
    status = await _run(
        EngineSwitcher(_FAST), _deps(sup, gw, FakeSettingsStore()), engines.FLASH_NEXT
    )
    assert status["stage"] == "done"
    assert any("drain timed out" in n and "gpt-oss-120b" in n for n in status["notes"])


@pytest.mark.asyncio
async def test_a_loading_model_or_an_unreadable_slot_counts_as_in_flight() -> None:
    sw = EngineSwitcher(_FAST)
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3.5-4b"})
    gw.states = {"qwen3.5-4b": "starting"}

    async def broken(served_model: str) -> list[dict[str, object]]:
        raise LocalGatewayError("no slots")

    assert await sw._in_flight(gw) == ["qwen3.5-4b"]  # type: ignore[arg-type]
    gw.slots = broken  # type: ignore[method-assign]
    assert await sw._in_flight(gw) == ["gpt-oss-120b", "qwen3.5-4b"]  # type: ignore[arg-type]
    assert await sw._in_flight(None) == []


@pytest.mark.asyncio
async def test_memory_settles_before_the_start_or_says_it_did_not() -> None:
    readings = iter([60.0, 30.0, 29.9])

    async def falling() -> float | None:
        return next(readings)

    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    timings = Timings(
        drain_s=0, settle_s=0, memory_settle_s=60, poll_s=0, gate_wait_s=0, engine_cache_s=0
    )
    deps = _deps(sup, FakeLocalGateway(), FakeSettingsStore(), memory_used_gb=falling)
    status = await _run(EngineSwitcher(timings), deps, engines.FLASH_NEXT)
    assert status["stage"] == "done" and not status["notes"]

    moving = iter([60.0, 50.0, 40.0])

    async def still_moving() -> float | None:
        return next(moving)

    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    deps = _deps(sup, FakeLocalGateway(), FakeSettingsStore(), memory_used_gb=still_moving)
    status = await _run(EngineSwitcher(_FAST), deps, engines.FLASH_NEXT)
    assert any("still moving" in n for n in status["notes"])


@pytest.mark.asyncio
async def test_the_nightly_guard_refuses_unless_forced() -> None:
    async def nightly() -> str | None:
        return "the scheduled wiki_build run fires in 12 min"

    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    store = FakeSettingsStore()
    deps = _deps(sup, FakeLocalGateway(), store, quiet_guard=nightly)
    sw = EngineSwitcher(_FAST)
    with pytest.raises(SwitchRefused) as exc:
        await sw.begin(deps, engines.FLASH_NEXT, source="owner")
    assert (
        exc.value.status == 409 and "wiki_build" in exc.value.detail and "force" in exc.value.detail
    )
    assert sup.events == [] and "llm_local_admission" not in store.values
    status = await _run(sw, deps, engines.FLASH_NEXT, force=True)
    assert status["stage"] == "done" and status["force"] is True


@pytest.mark.asyncio
async def test_refusals_touch_nothing() -> None:
    store = FakeSettingsStore()
    sw = EngineSwitcher(_FAST)
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    sup.busy = "update"
    with pytest.raises(SwitchRefused, match="update"):
        await sw.begin(_deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT, source="owner")
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    with pytest.raises(SwitchRefused, match="weights are not installed"):
        await sw.begin(
            _deps(sup, FakeLocalGateway(), store, local_models=["gpt-oss-120b"]),
            engines.FLASH_NEXT,
            source="owner",
        )
    sup.unreachable = True
    with pytest.raises(SwitchRefused) as exc:
        await sw.begin(_deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT, source="owner")
    assert exc.value.status == 502
    assert sup.events == [] and not sw.busy and "llm_local_admission" not in store.values


@pytest.mark.asyncio
async def test_a_second_switch_while_one_runs_is_a_409() -> None:
    release = asyncio.Event()

    class _SlowLoad(FakeLocalGateway):
        async def load(self, served_model: str, **kw: Any) -> None:
            await release.wait()
            await super().load(served_model)

    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    deps = _deps(sup, _SlowLoad(), FakeSettingsStore())
    sw = EngineSwitcher(_FAST)
    first = await sw.begin(deps, engines.FLASH_NEXT, source="owner")
    for _ in range(20):
        await asyncio.sleep(0)
    with pytest.raises(SwitchRefused, match="already in progress"):
        await sw.begin(deps, engines.STANDARD, source="debug")
    live = await sw.status(deps)
    assert live is not None and live["id"] == first["id"] and live["stage"] == "loading"
    release.set()
    await sw.wait()
    assert not sw.busy


@pytest.mark.asyncio
async def test_a_crash_inside_the_switch_fails_it_and_reopens_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    store = FakeSettingsStore()
    sw = EngineSwitcher(_FAST)

    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sw, "_unload_resident", boom)
    status = await _run(sw, _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "failed" and "crashed" in status["reason"]
    assert store.values["llm_local_admission"] == drain.OPEN_ROW and not sw.busy


@pytest.mark.asyncio
async def test_a_switch_with_nothing_installed_on_standard_skips_the_smoke() -> None:
    sup = _Sup({"local-llm": "exited", "flash-next": "running"})
    store = FakeSettingsStore()
    status = await _run(
        EngineSwitcher(_FAST),
        _deps(sup, FakeLocalGateway(), store, local_models=[FN]),
        engines.STANDARD,
    )
    assert status["stage"] == "done" and "smoke test skipped" in status["notes"][0]


def test_smoke_model_choice() -> None:
    flash = smoke_model(engines.FLASH_NEXT, _INSTALLED, None)
    assert flash is not None and flash.id == FN
    assert smoke_model(engines.FLASH_NEXT, ["gpt-oss-120b"], None) is None
    primary = smoke_model(engines.STANDARD, _INSTALLED, "gpt-oss-120b")
    assert primary is not None and primary.id == "gpt-oss-120b"
    smallest = smoke_model(engines.STANDARD, _INSTALLED, None)
    assert smallest is not None and smallest.id == "qwen3.5-4b"


@pytest.mark.asyncio
async def test_a_restart_reopens_admission_and_marks_an_unfinished_switch() -> None:
    store = FakeSettingsStore()
    await store.set_llm_local_admission(None, drain.closed_row("x", ttl_s=600, now=time.time()))
    await store.set_llm_local_engine_switch(None, {"id": "a", "stage": "loading"})
    await reset_after_restart(store, None)  # type: ignore[arg-type]
    assert store.values["llm_local_admission"] == drain.OPEN_ROW
    switch = store.values["llm_local_engine_switch"]
    assert isinstance(switch, dict) and switch["stage"] == "failed"
    assert "interrupted" in switch["reason"]
    await store.set_llm_local_engine_switch(None, {"id": "b", "stage": "done"})
    await reset_after_restart(store, None)  # type: ignore[arg-type]
    assert store.values["llm_local_engine_switch"] == {"id": "b", "stage": "done"}


# --- the nightly window (pure) -------------------------------------------------------------


def test_the_nightly_window() -> None:
    now = datetime(2026, 10, 2, 1, 45, tzinfo=UTC)

    def sched(**kw: Any) -> ScheduleWindow:
        base: dict[str, Any] = {
            "label": "wiki_build",
            "schedule_kind": "interval",
            "interval_seconds": 86400,
            "next_run_at": None,
            "last_run_at": None,
        }
        return ScheduleWindow(**{**base, **kw})

    soon = sched(next_run_at=now + timedelta(minutes=15))
    assert nightly_window_reason([soon], now) == (
        "the scheduled wiki_build run fires in 15 min (at 02:00 UTC)"
    )
    # Rendered in the owner's zone.
    tz = ZoneInfo("America/New_York")
    assert "(at 22:00 EDT)" in str(nightly_window_reason([soon], now, tz=tz))
    # Due but not yet picked up by the tick: still the window.
    overdue = sched(next_run_at=now - timedelta(minutes=5))
    assert "is due now" in str(nightly_window_reason([overdue], now))
    later = sched(next_run_at=now + timedelta(hours=2))
    assert nightly_window_reason([later], now) is None
    running = sched(schedule_kind="repeat", last_run_at=now - timedelta(minutes=20))
    reason = nightly_window_reason([running], now)
    assert reason is not None and "started at 01:25 UTC" in reason
    old = sched(last_run_at=now - timedelta(hours=3))
    assert nightly_window_reason([old], now) is None
    reconciler = sched(interval_seconds=300, next_run_at=now + timedelta(minutes=1))
    assert nightly_window_reason([reconciler], now) is None


# --- the owner routes ----------------------------------------------------------------------


@pytest.fixture
def owner_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    async def quiet(maker: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(engine_api, "quiet_window_guard", quiet)
    settings = Settings(
        secure_cookies=False,
        database_url=_DB,
        xai_api_key="test-xai",
        anthropic_api_key="test-anthropic",
        supervisor_token="sek",
        local_models_dir=str(tmp_path),
        local_llm_enabled=True,
        local_models=list(_INSTALLED),
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
        app.state.residency = None
        app.state.engine_switcher = EngineSwitcher(_FAST)
        key = asyncio.run(auth_service.rotate_owner_key(repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, app


def test_owner_routes_need_the_owner() -> None:
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/llm/engine").status_code == 401
        assert anon.post("/api/settings/llm/engine", json={"engine": "standard"}).status_code == 401
        app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
            id="cap", kind="capability_token", label="token"
        )
        assert anon.get("/api/settings/llm/engine").status_code == 403
        assert anon.post("/api/settings/llm/engine", json={"engine": "standard"}).status_code == 403


def _poll(client: TestClient, switch_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get("/api/settings/llm/engine").json()
        status = body.get("switch")
        if status and status["id"] == switch_id and status["stage"] in _TERMINAL:
            return body
        time.sleep(0.005)
    raise AssertionError("the switch never ended")


def test_owner_switch_end_to_end(owner_box: tuple[TestClient, Any]) -> None:
    client, app = owner_box
    before = client.get("/api/settings/llm/engine").json()
    assert before["effective"] == "standard" and before["installed"] == {
        "standard": True,
        "flash-next": True,
    }
    assert before["switch"] is None and before["admission"]["closed"] is False
    assert before["memory"]["gtt_used_gb"] is None and before["guard"] is None

    resp = client.post("/api/settings/llm/engine", json={"engine": "flash-next"})
    assert resp.status_code == 202 and resp.json()["source"] == "owner"
    body = _poll(client, resp.json()["id"])
    assert body["switch"]["stage"] == "done" and body["effective"] == "flash-next"
    assert body["running"] == ["flash-next"] and body["consistent"] is True
    assert app.state.supervisor_client.violations == []


def test_owner_switch_respects_the_nightly_guard_and_force(
    owner_box: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = owner_box

    async def nightly(maker: object, **_kw: object) -> str:
        return "the workflow run 'nightly_sweep' is executing"

    monkeypatch.setattr(engine_api, "quiet_window_guard", nightly)
    assert client.get("/api/settings/llm/engine").json()["guard"].endswith("is executing")
    resp = client.post("/api/settings/llm/engine", json={"engine": "flash-next"})
    assert resp.status_code == 409 and "nightly_sweep" in resp.json()["detail"]
    resp = client.post("/api/settings/llm/engine", json={"engine": "flash-next", "force": True})
    assert resp.status_code == 202
    assert _poll(client, resp.json()["id"])["switch"]["force"] is True


def test_owner_read_is_502_on_an_unreachable_supervisor(owner_box: tuple[TestClient, Any]) -> None:
    client, app = owner_box
    real, app.state.supervisor_client = app.state.supervisor_client, None
    try:
        assert client.get("/api/settings/llm/engine").status_code == 502
    finally:
        app.state.supervisor_client = real


@pytest.mark.asyncio
async def test_admission_reopens_only_after_other_processes_engine_caches_expire() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []
    store = FakeSettingsStore()

    async def _sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    sw = EngineSwitcher(
        Timings(
            drain_s=0, settle_s=0, memory_settle_s=0, poll_s=0, gate_wait_s=0, engine_cache_s=5
        ),
        sleep=_sleep,
        clock=lambda: clock["t"],
    )
    sup = _Sup({"local-llm": "running", "flash-next": "exited"})
    status = await _run(sw, _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "done" and 5 in slept
    assert store.values["llm_local_admission"] == drain.OPEN_ROW


def test_owner_read_reports_device_memory(owner_box: tuple[TestClient, Any]) -> None:
    from jbrain.llm.gpu_guard import GpuMem

    client, app = owner_box

    class _Probe:
        async def sample(self) -> GpuMem:
            return GpuMem(gtt_used_gb=80.0, gtt_total_gb=120.0, vram_used_gb=0, vram_total_gb=0)

    app.state.gpu_probe = _Probe()
    memory = client.get("/api/settings/llm/engine").json()["memory"]
    assert memory["gtt_used_gb"] == 80.0 and memory["gtt_free_gb"] == 40.0


# --- the settings snapshot: Load button + per-task marker ----------------------------------


def test_snapshot_marks_loadable_and_the_remap(owner_box: tuple[TestClient, Any]) -> None:
    client, app = owner_box
    store: FakeSettingsStore = app.state.settings_store
    store.values["llm_task_overrides"] = {
        "agent.turn": {"spec": "local:gpt-oss-120b", "reasoning_effort": "low"},
        "wiki.rewrite": {"spec": f"local:{FN}", "reasoning_effort": "low"},
    }
    body = client.get("/api/settings/llm").json()
    models = {m["id"]: m for m in body["local_models"]}
    assert models["gpt-oss-120b"]["loadable_now"] is True
    assert models[FN]["loadable_now"] is False
    blocked = "Runs on the Flash-Next engine — switch engines to load it"
    assert models[FN]["blocked_reason"] == blocked
    assert models["llama-3.3-70b"]["blocked_reason"].startswith("Not installed")
    tasks = {t["id"]: t for t in body["tasks"]}
    assert tasks["agent.turn"]["remapped"] is False
    assert tasks["agent.turn"]["effective_spec"] == "local:gpt-oss-120b"
    assert tasks["wiki.rewrite"]["remapped"] is True
    assert tasks["wiki.rewrite"]["effective_spec"] == "xai:grok-4.3"
    assert tasks["wiki.rewrite"]["remap_note"] == "→ task default (Flash-Next is off)"
    # The Load button's 409 carries the snapshot's own sentence.
    resp = client.post(f"/api/settings/llm/local-models/{FN}/load")
    assert resp.status_code == 409 and resp.json()["detail"] == blocked

    store.values["llm_local_engine_effective"] = "flash-next"
    body = client.get("/api/settings/llm").json()
    models = {m["id"]: m for m in body["local_models"]}
    assert models[FN]["loadable_now"] is True
    assert models["gpt-oss-120b"]["blocked_reason"] == (
        "Runs on the Standard engine — switch engines to load it"
    )
    tasks = {t["id"]: t for t in body["tasks"]}
    assert tasks["agent.turn"]["effective_spec"] == f"local:{FN}"
    assert tasks["agent.turn"]["remap_note"] == "→ Flash-Next (engine active)"
    assert tasks["fact.adjudicate"]["remapped"] is False  # a cloud route is never remapped

    store.values["llm_local_admission"] = drain.closed_row("s", ttl_s=600, now=time.time())
    models = {m["id"]: m for m in client.get("/api/settings/llm").json()["local_models"]}
    assert models[FN]["blocked_reason"] == llm_settings.SWITCHING_REASON


def test_chat_capabilities_describe_the_model_that_runs(
    owner_box: tuple[TestClient, Any],
) -> None:
    client, app = owner_box
    store: FakeSettingsStore = app.state.settings_store
    store.values["llm_task_overrides"] = {"agent.turn": {"spec": "local:gpt-oss-120b"}}
    caps = client.get("/api/chat/capabilities").json()
    assert caps["supports_vision"] is False
    store.values["llm_local_engine_effective"] = "flash-next"
    caps = client.get("/api/chat/capabilities").json()
    assert caps["supports_vision"] is True and caps["context_window"] == 262144


# --- the jcode proxy -----------------------------------------------------------------------


def _jcode_app(engine_now: dict[str, Any], sent: dict[str, Any]) -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(jcode_llm.router, prefix="/api")
    app.state.settings = SimpleNamespace(
        jcode_gateway_token="t",
        local_llm_enabled=True,
        local_models=_INSTALLED,
        local_llm_url="http://gw/v1",
        jcode_model="gpt-oss-120b",
    )
    app.state.active_engine = engines.ActiveEngine(_engine_loader(engine_now), ttl_s=0.0)
    calls: list[str] = []

    class _Res:
        async def ensure_room(self, served: str) -> None:
            calls.append(served)

    app.state.residency = _Res()
    sent["admitted"] = calls

    class _Stream:
        async def __aenter__(self) -> _Stream:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def aiter_raw(self):  # noqa: ANN202
            yield b"{}"

    class _Client:
        def __init__(self, **kw: object) -> None:
            pass

        def stream(self, method: str, path: str, *, json: object, **_k: object) -> _Stream:
            sent["payload"] = json
            return _Stream()

        async def aclose(self) -> None:
            return None

    app.state.jcode_llm_client_factory = _Client
    return TestClient(app)


def test_jcode_proxy_remaps_a_stale_name_onto_flash_next() -> None:
    sent: dict[str, Any] = {}
    client = _jcode_app({"engine": engines.FLASH_NEXT}, sent)
    resp = client.post(
        "/api/jcode/llm/v1/chat/completions",
        headers={"Authorization": "Bearer t"},
        json={"model": "gpt-oss-120b", "messages": []},
    )
    assert resp.status_code == 200
    assert sent["payload"]["model"] == FN and sent["admitted"] == [FN]


def test_jcode_proxy_falls_back_from_flash_next_to_the_configured_model() -> None:
    sent: dict[str, Any] = {}
    client = _jcode_app({"engine": engines.STANDARD}, sent)
    resp = client.post(
        "/api/jcode/llm/v1/chat/completions",
        headers={"Authorization": "Bearer t"},
        json={"model": FN, "messages": []},
    )
    assert resp.status_code == 200 and sent["payload"]["model"] == "gpt-oss-120b"


# --- the gateway's smoke probes ------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_and_image_probes_are_thinking_off_and_resident_only() -> None:
    bodies: list[dict[str, Any]] = []
    answer: dict[str, Any] = {"content": "OK"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": FN, "state": "ready"}]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": answer}]})

    client = LocalGatewayClient("http://gw/v1", transport=httpx.MockTransport(handler))
    assert await client.text_probe(FN) == "OK"
    assert await client.image_probe(FN) == "OK"
    assert bodies[0]["chat_template_kwargs"]["enable_thinking"] is False
    image = bodies[1]["messages"][0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,iVBORw0KGgo")
    answer.clear()
    with pytest.raises(LocalGatewayError, match="answered with nothing"):
        await client.text_probe(FN)
    with pytest.raises(LocalGatewayError, match="not resident"):
        await client.text_probe("gpt-oss-120b")


# --- review round: the hold, every failure branch, the admitted name, deadlines -------------


def _sw() -> EngineSwitcher:
    return EngineSwitcher(_FAST)


def _standard_up() -> _Sup:
    return _Sup({"local-llm": "running", "flash-next": "exited"})


@pytest.mark.asyncio
async def test_the_switch_holds_the_supervisor_and_its_own_start_carries_the_id() -> None:
    sup = _standard_up()
    status = await _run(
        _sw(), _deps(sup, FakeLocalGateway(), FakeSettingsStore()), engines.FLASH_NEXT
    )
    assert status["stage"] == "done"
    held_ids = {h[0] for h in sup.holds}
    assert held_ids == {status["id"]} and len(sup.holds) >= 5  # taken, then renewed per stage
    assert sup.start_ids == [status["id"]]
    assert sup.released == [status["id"]]


@pytest.mark.asyncio
async def test_a_refused_hold_refuses_the_switch_and_touches_nothing() -> None:
    store = FakeSettingsStore()
    for answer, code in (("refused", 409), ("error", 502)):
        sup = _standard_up()
        sup.hold_answer = answer
        sw = _sw()
        with pytest.raises(SwitchRefused) as exc:
            await sw.begin(_deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT, source="o")
        assert exc.value.status == code and not sw.busy
        assert sup.events == [] and "llm_local_admission" not in store.values


@pytest.mark.asyncio
async def test_an_older_supervisor_without_the_hold_still_switches_and_says_so() -> None:
    sup = _standard_up()
    sup.hold_answer = "missing"
    status = await _run(
        _sw(), _deps(sup, FakeLocalGateway(), FakeSettingsStore()), engines.FLASH_NEXT
    )
    assert status["stage"] == "done"
    assert any("predates the switch hold" in n for n in status["notes"])


@pytest.mark.asyncio
async def test_a_oneshot_that_appears_mid_switch_stops_the_start(recorded: list) -> None:
    sup = _standard_up()
    sup.busy_after_preflight = "update"
    store = FakeSettingsStore()
    status = await _run(_sw(), _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "failed"
    assert "update one-shot started" in status["reason"] and "NOT started" in status["reason"]
    assert "start flash-next" not in sup.events
    # Nothing is up and the update owns the box: said plainly, and recorded.
    assert "NO local engine is up" in status["reason"]
    assert store.values["llm_local_engine_effective"] == "standard"
    assert recorded[-1][3] == "failed"


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_is_reissued_and_what_is_up_is_recorded() -> None:
    sup = _standard_up()
    sup.stuck.add("local-llm")
    store = FakeSettingsStore()
    status = await _run(_sw(), _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "failed"
    assert sup.events == ["stop local-llm", "stop local-llm"]
    assert (
        "local-llm did not stop" in status["reason"] and "standard is serving" in status["reason"]
    )
    assert store.values["llm_local_engine_effective"] == "standard"


@pytest.mark.asyncio
async def test_a_refused_stop_of_the_other_engine_starts_nothing() -> None:
    sup = _standard_up()
    sup.stop_error.add("local-llm")
    status = await _run(
        _sw(), _deps(sup, FakeLocalGateway(), FakeSettingsStore()), engines.FLASH_NEXT
    )
    assert status["stage"] == "failed" and "start flash-next" not in sup.events


@pytest.mark.asyncio
async def test_a_stop_that_takes_but_never_confirms_with_nothing_up_puts_previous_back() -> None:
    """The stop is refused and /status was unreadable while waiting, then shows it down:
    the previous engine is restarted once it is confirmed down."""
    sup = _standard_up()
    sup.stop_error.add("local-llm")
    sup.state["local-llm"] = "running"
    reads = {"n": 0}
    real_states = sup.states

    async def flaky_states() -> dict[str, str]:
        reads["n"] += 1
        if reads["n"] == 4:  # after both stop attempts gave up, it has gone down
            sup.state["local-llm"] = "exited"
        if 2 <= reads["n"] < 4:
            raise SupervisorError("no route")
        return await real_states()

    sup.states = flaky_states  # type: ignore[method-assign]
    store = FakeSettingsStore()
    status = await _run(_sw(), _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "rolled_back" and "standard was put back" in status["reason"]
    assert (
        sup.events[-1] == "start local-llm"
        and store.values["llm_local_engine_effective"] == "standard"
    )


@pytest.mark.asyncio
async def test_rollback_with_the_target_not_confirmed_down_restores_nothing() -> None:
    sup = _standard_up()
    gw = FakeLocalGateway(fail_load=True)
    sup.stuck.add("flash-next")
    store = FakeSettingsStore()
    status = await _run(_sw(), _deps(sup, gw, store), engines.FLASH_NEXT)
    assert status["stage"] == "failed" and "nothing was restored" in status["reason"]
    assert "start local-llm" not in sup.events
    assert store.values["llm_local_engine_effective"] == "flash-next"  # what IS up
    meta = store.values["llm_local_engine_effective_meta"]
    assert isinstance(meta, dict) and "nothing was restored" in str(meta["reason"])


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["refused", "crash"])
async def test_a_restore_that_fails_says_no_local_engine_is_up(recorded: list, how: str) -> None:
    sup = _standard_up()
    sup.start_404.add("flash-next")
    if how == "refused":
        sup.start_error.add("local-llm")
    else:
        sup.start_crash.add("local-llm")
    store = FakeSettingsStore()
    status = await _run(_sw(), _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "failed"
    assert "could NOT be put back" in status["reason"]
    assert "NO local engine is up" in status["reason"]
    assert status["no_engine_up"] is True
    assert store.values["llm_local_engine_effective"] == "standard"
    assert "NO local engine is up" in str(recorded[-1][2])


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["404", "error", "crash"])
async def test_every_start_failure_rolls_back(how: str) -> None:
    sup = _standard_up()
    {"404": sup.start_404, "error": sup.start_error, "crash": sup.start_crash}[how].add(
        "flash-next"
    )
    store = FakeSettingsStore()
    status = await _run(_sw(), _deps(sup, FakeLocalGateway(), store), engines.FLASH_NEXT)
    assert status["stage"] == "rolled_back" and "standard was put back" in status["reason"]
    assert sup.events[-1] == "start local-llm"
    assert store.values["llm_local_engine_effective"] == "standard"


@pytest.mark.asyncio
async def test_an_unload_failure_names_what_was_already_released() -> None:
    class _HalfUnload(FakeLocalGateway):
        async def unload(self, served_model: str) -> None:
            if served_model == "qwen3.5-4b":
                raise LocalGatewayError("stuck")
            await super().unload(served_model)

    sup = _standard_up()
    gw = _HalfUnload(running={"gpt-oss-120b", "qwen3.5-4b"})
    status = await _run(_sw(), _deps(sup, gw, FakeSettingsStore()), engines.FLASH_NEXT)
    assert status["stage"] == "failed"
    assert "could not unload qwen3.5-4b (stuck)" in status["reason"]
    assert "already unloaded" in status["reason"] and "gpt-oss-120b" in status["reason"]
    assert sup.events == []


@pytest.mark.asyncio
async def test_admission_and_the_hold_are_extended_at_every_stage() -> None:
    wall = {"t": 1_000.0}
    rows: list[object] = []

    class _Store(FakeSettingsStore):
        async def set_llm_local_admission(self, ctx: object, row: dict[str, object]) -> None:
            rows.append(dict(row))
            wall["t"] += 100.0  # every stage write lands later
            await super().set_llm_local_admission(ctx, row)

    sw = EngineSwitcher(_FAST, wall=lambda: wall["t"])
    sup = _standard_up()
    status = await _run(sw, _deps(sup, FakeLocalGateway(), _Store()), engines.FLASH_NEXT)
    assert status["stage"] == "done"
    closed = [r for r in rows if isinstance(r, dict) and r.get("closed")]
    untils = [float(r["until"]) for r in closed]  # type: ignore[arg-type]
    assert len(closed) >= 5 and untils == sorted(untils) and untils[-1] > untils[0]
    # Never reopened until the very end.
    assert rows[-1] == drain.OPEN_ROW and all(r != drain.OPEN_ROW for r in rows[:-1])


@pytest.mark.asyncio
async def test_a_guard_that_cannot_be_read_fails_closed() -> None:
    async def broken() -> str | None:
        raise RuntimeError("db down")

    sup = _standard_up()
    deps = _deps(sup, FakeLocalGateway(), FakeSettingsStore(), quiet_guard=broken)
    with pytest.raises(SwitchRefused) as exc:
        await _sw().begin(deps, engines.FLASH_NEXT, source="owner")
    assert exc.value.status == 409 and "force" in exc.value.detail and "db down" in exc.value.detail
    status = await _run(_sw(), deps, engines.FLASH_NEXT, force=True)
    assert status["stage"] == "done"


@pytest.mark.asyncio
async def test_a_failed_box_event_still_ends_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*a: object, **k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(box_events, "record", broken)
    sw = _sw()
    status = await _run(
        sw, _deps(_standard_up(), FakeLocalGateway(), FakeSettingsStore()), engines.FLASH_NEXT
    )
    assert status["stage"] == "done" and not sw.busy


@pytest.mark.asyncio
async def test_concurrent_gate_reads_share_one_load() -> None:
    loads = {"n": 0}
    release = asyncio.Event()

    async def slow() -> object:
        loads["n"] += 1
        await release.wait()
        return drain.OPEN_ROW

    gate = drain.AdmissionGate(slow, ttl_s=60.0)
    readers = [asyncio.create_task(gate.closure()) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*readers) == [None] * 5
    assert loads["n"] == 1


def test_the_drain_waits_three_seconds_after_closing() -> None:
    assert Timings().gate_wait_s == 3.0


# --- the router and residency agree on the name they admit and send -------------------------


class _RemappingAdmit:
    """Residency that admits something other than what it was asked for — the engine changed
    between the router's read and its own."""

    def __init__(self, admitted: str, flip: dict[str, Any] | None = None) -> None:
        self.admitted = admitted
        self.flip = flip

    async def ensure_room(self, served_model: str) -> str:
        if self.flip is not None:
            self.flip["engine"] = engines.FLASH_NEXT
        return self.admitted


@pytest.mark.asyncio
async def test_the_router_sends_what_residency_admitted_after_a_switch() -> None:
    engine_now: dict[str, Any] = {"engine": engines.STANDARD}
    router, local, _, _ = _router(engine_now, {"agent.turn": {"spec": "local:gpt-oss-120b"}})
    router._residency = _RemappingAdmit(FN, engine_now)
    await router.complete("agent.turn", system="s", user_text="u")
    # Re-resolved on the new engine: Flash-Next with ITS effort and sampling.
    assert local.calls[-1]["model"] == FN
    flash = local_catalog.get(FN)
    assert flash is not None and local.calls[-1]["sampling"] == flash.sampling_thinking


@pytest.mark.asyncio
async def test_the_router_sends_the_admitted_name_when_a_resolve_still_disagrees() -> None:
    engine_now: dict[str, Any] = {"engine": engines.FLASH_NEXT}
    router, local, _, _ = _router(engine_now, {"agent.turn": {"spec": "local:gpt-oss-120b"}})
    router._residency = _RemappingAdmit("qwen3-30b-a3b")
    await router.complete("agent.turn", system="s", user_text="u")
    assert local.calls[-1]["model"] == "qwen3-30b-a3b"
    assert local.calls[-1]["reasoning_effort"] is None  # re-gated: a non-reasoning model


@pytest.mark.asyncio
async def test_residency_returns_the_name_it_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    gw = FakeLocalGateway(running={FN})
    assert await _coord({"engine": engines.FLASH_NEXT}, gw).ensure_room("gpt-oss-120b") == FN
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False))
    assert await coord.ensure_room("gpt-oss-120b") == "gpt-oss-120b"


def test_the_jcode_proxy_sends_what_residency_admitted() -> None:
    sent: dict[str, Any] = {}
    client = _jcode_app({"engine": engines.STANDARD}, sent)
    app = client.app

    class _Flip:
        async def ensure_room(self, served: str) -> str:
            return FN

    app.state.residency = _Flip()  # type: ignore[attr-defined]
    resp = client.post(
        "/api/jcode/llm/v1/chat/completions",
        headers={"Authorization": "Bearer t"},
        json={"model": "gpt-oss-120b", "messages": []},
    )
    assert resp.status_code == 200 and sent["payload"]["model"] == FN


# --- api routes refuse engine-affecting work while a switch runs ----------------------------


def test_engine_affecting_routes_refuse_while_a_switch_runs(
    owner_box: tuple[TestClient, Any],
) -> None:
    client, app = owner_box
    switcher: EngineSwitcher = app.state.engine_switcher
    asyncio.run(switcher._lock.acquire())
    try:
        for method, path, body in (
            ("POST", "/api/ops/update", None),
            ("POST", "/api/ops/rebuild", {"service": "api"}),
            ("POST", "/api/ops/restart", {"service": "local-llm"}),
            ("POST", "/api/ops/restart", {"service": "all"}),
            ("POST", "/api/ops/start", {"service": "flash-next"}),
            ("POST", "/api/ops/local-provision", None),
            ("POST", "/api/ops/export", None),
            ("POST", "/api/ops/reset", None),
            ("POST", "/api/jcode/power", {"on": True}),
            ("POST", "/api/jcode/model/warm", None),
        ):
            resp = client.request(method, path, json=body)
            assert resp.status_code == 409, (path, resp.status_code, resp.text)
            assert "engine switch is in progress" in resp.json()["detail"]
        # A non-engine container is not the switch's business.
        app.state.supervisor_client.states["api"] = "running"
        assert client.post("/api/ops/start", json={"service": "api"}).status_code != 409
    finally:
        switcher._lock.release()
    assert all(
        not e.startswith(("start", "restart")) or "api" in e
        for e in app.state.supervisor_client.events
    )


# --- the engine card's extra fields, the cancel, the weights text ---------------------------


def test_effective_since_and_the_fallback_reason(owner_box: tuple[TestClient, Any]) -> None:
    client, app = owner_box
    store: FakeSettingsStore = app.state.settings_store
    store.values["llm_local_engine"] = "flash-next"
    asyncio.run(
        store.set_llm_local_engine_effective(
            None, "standard", reason="the Flash-Next engine did not start on the last update"
        )
    )
    body = client.get("/api/settings/llm/engine").json()
    assert body["effective_since"] and body["fallback_reason"].startswith("the Flash-Next")
    store.values["llm_local_engine"] = "standard"
    assert client.get("/api/settings/llm/engine").json()["fallback_reason"] is None


def test_decode_tps_reads_the_resident_models_gauge(owner_box: tuple[TestClient, Any]) -> None:
    client, app = owner_box
    app.state.local_gateway.metrics_text = (
        "# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.\n"
        "llamacpp:predicted_tokens_seconds 31.47\n"
    )
    body = client.get("/api/settings/llm/engine").json()
    assert body["decode_tps"] == 31.5 and body["decode_model"] == "gpt-oss-120b"
    app.state.local_gateway = FakeLocalGateway()
    body = client.get("/api/settings/llm/engine").json()
    assert body["decode_tps"] is None and body["decode_model"] is None
    assert engine_api.parse_decode_tps("llamacpp:predicted_tokens_seconds 0\n") is None
    assert engine_api.parse_decode_tps("") is None


@pytest.mark.asyncio
async def test_a_draining_switch_can_be_cancelled_and_nothing_stops(recorded: list) -> None:
    release = asyncio.Event()

    class _Busy(FakeLocalGateway):
        async def slots(self, served_model: str) -> list[dict[str, object]]:
            await release.wait()
            return [{"id": 0, "is_processing": True}]

    sup = _standard_up()
    store = FakeSettingsStore()
    sw = EngineSwitcher(
        Timings(
            drain_s=60, settle_s=0, memory_settle_s=0, poll_s=0, gate_wait_s=0, engine_cache_s=0
        )
    )
    deps = _deps(sup, _Busy(running={"gpt-oss-120b"}), store)
    with pytest.raises(SwitchRefused):
        sw.cancel()  # nothing running
    await sw.begin(deps, engines.FLASH_NEXT, source="owner")
    for _ in range(5):
        await asyncio.sleep(0)
    sw.cancel()
    release.set()
    await sw.wait()
    status = await sw.status(deps)
    assert status is not None and status["stage"] == "cancelled"
    assert sup.events == [] and store.values["llm_local_admission"] == drain.OPEN_ROW
    assert "llm_local_engine" not in store.values
    assert recorded[-1][0] == box_events.ENGINE_SWITCH and "cancelled" in str(recorded[-1][2])


@pytest.mark.asyncio
async def test_a_switch_past_draining_cannot_be_cancelled() -> None:
    release = asyncio.Event()

    class _SlowLoad(FakeLocalGateway):
        async def load(self, served_model: str, **kw: Any) -> None:
            await release.wait()
            await super().load(served_model)

    sw = _sw()
    deps = _deps(_standard_up(), _SlowLoad(), FakeSettingsStore())
    await sw.begin(deps, engines.FLASH_NEXT, source="owner")
    for _ in range(30):
        await asyncio.sleep(0)
    with pytest.raises(SwitchRefused, match="stage: loading"):
        sw.cancel()
    release.set()
    await sw.wait()


def test_cancel_routes(owner_box: tuple[TestClient, Any]) -> None:
    client, _ = owner_box
    resp = client.post("/api/settings/llm/engine/cancel")
    assert resp.status_code == 409 and "draining" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_weights_point_at_on_box_models() -> None:
    with pytest.raises(SwitchRefused, match="under On-box models first"):
        await _sw().begin(
            _deps(_standard_up(), FakeLocalGateway(), FakeSettingsStore(), local_models=[]),
            engines.FLASH_NEXT,
            source="owner",
        )
