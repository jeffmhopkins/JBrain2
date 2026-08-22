"""Router behavior: task resolution, the JSON re-ask, and build_router wiring."""

import asyncio
import json
from typing import cast

import httpx
import pytest

from jbrain.config import Settings
from jbrain.llm import (
    FakeLlmClient,
    LlmBadResponseError,
    LlmClient,
    LlmError,
    LlmRouter,
    LlmTurn,
    LlmUsage,
    Sampling,
    build_router,
)
from jbrain.llm.router import CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW, JSON_NUDGE


def _inert_residency() -> object:
    """A disabled coordinator, for the tests that only care about routing.

    `residency` is required on build_router (see
    `test_build_router_requires_a_residency_admitter`), and disabled is the honest thing
    for a test that never loads a model — not a stub that silently admits everything,
    which is the exact failure the required parameter exists to prevent."""
    from jbrain.llm.residency import ResidencyCoordinator

    return ResidencyCoordinator(object(), enabled=False)  # type: ignore[arg-type]


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def fake_router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter({"xai": fake}, {"note.extract": ("xai", "grok-4.3")})


async def test_complete_routes_task_to_provider_model() -> None:
    fake = FakeLlmClient(["fine"])
    result = await fake_router(fake).complete("note.extract", system="s", user_text="u")
    assert result.text == "fine"
    assert fake.calls[0]["model"] == "grok-4.3"
    assert fake.calls[0]["system"] == "s"


class _FakeResidency:
    """Records the served-model names the router admits (the LocalAdmitter shape)."""

    def __init__(self) -> None:
        self.admitted: list[str] = []

    async def ensure_room(self, served_model: str) -> None:
        self.admitted.append(served_model)


async def test_local_admit_runs_before_a_local_completion() -> None:
    # A local model's completion first gives the residency budget a chance to evict for it
    # (the served-model name), then delegates to the client — co-residency admission.
    fake = FakeLlmClient(["ok"])
    residency = _FakeResidency()

    router = LlmRouter(
        {"local": fake}, {"agent.turn": ("local", "qwen3.5-4b")}, residency=residency
    )
    await router.complete("agent.turn", system="s", user_text="u")
    assert residency.admitted == ["qwen3.5-4b"]
    assert fake.calls[0]["model"] == "qwen3.5-4b"


async def test_local_admit_not_called_for_a_cloud_completion() -> None:
    fake = FakeLlmClient(["ok"])
    residency = _FakeResidency()

    router = LlmRouter({"xai": fake}, {"note.extract": ("xai", "grok-4.3")}, residency=residency)
    await router.complete("note.extract", system="s", user_text="u")
    assert residency.admitted == []  # cloud models never touch the local residency budget


async def test_local_swap_evicts_the_resident_model_through_the_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavior that actually crashed the box: a local completion whose model can't
    co-reside with the resident one drives a REAL eviction through the router's admission.
    Pins router → ResidencyCoordinator → gateway.unload end to end (not just that admission
    is wired). The vision model is resident; the summary model can't fit beside it under the
    free-RAM floor, so admitting the summary completion unloads the vision model first —
    exactly the vision→reasoning swap the video worker makes."""
    from jbrain.llm.residency import ResidencyCoordinator
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b"})
    # 121 GB box; `used` already reflects the ~33 GB resident vision model plus ~12 GB base.
    # gpt-oss-120b (~63 GB) pushes past the 12.5%-free floor (ceiling ≈ 105.9 GB), so the
    # coordinator must evict the vision model before the summary completion runs.
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (121.0, 45.0)
    )
    residency = ResidencyCoordinator(gw, models_dir="", enabled=True, free_ram_fraction=0.125)
    fake = FakeLlmClient(["caption", "summary"])
    router = LlmRouter(
        {"local": fake},
        {
            "agent.vision": ("local", "qwen3-vl-30b-a3b"),
            "video.summarize": ("local", "gpt-oss-120b"),
        },
        residency=residency,
    )
    # The vision model is already resident, so captioning evicts nothing.
    await router.complete("agent.vision", system="s", user_text="frame")
    assert gw.unloaded == []
    # Summarizing needs the reasoning model, which can't co-reside — the vision model is
    # unloaded first, through the router. Without admission this is the ~100 GB co-load OOM.
    await router.complete("video.summarize", system="s", user_text="timeline")
    assert gw.unloaded == ["qwen3-vl-30b-a3b"]


async def test_unknown_task_raises() -> None:
    with pytest.raises(LlmError, match="unknown LLM task"):
        await fake_router(FakeLlmClient()).complete("nope", system="s", user_text="u")


async def test_json_reask_nudges_once_then_succeeds() -> None:
    fake = FakeLlmClient(["this is prose, not JSON", '{"ok": true}'])
    result = await fake_router(fake).complete(
        "note.extract", system="s", user_text="u", json_schema=SCHEMA
    )
    assert result.parsed == {"ok": True}
    assert len(fake.calls) == 2
    assert fake.calls[1]["user_text"] == "u" + JSON_NUDGE


async def test_json_reask_failure_raises_bad_response() -> None:
    fake = FakeLlmClient(["nope", "still nope"])
    with pytest.raises(LlmBadResponseError, match="after re-ask"):
        await fake_router(fake).complete(
            "note.extract", system="s", user_text="u", json_schema=SCHEMA
        )
    assert len(fake.calls) == 2


async def test_valid_json_needs_no_reask() -> None:
    fake = FakeLlmClient(['{"ok": false}'])
    result = await fake_router(fake).complete(
        "note.extract", system="s", user_text="u", json_schema=SCHEMA
    )
    assert result.parsed == {"ok": False}
    assert len(fake.calls) == 1


async def test_no_schema_means_no_parse_and_no_reask() -> None:
    fake = FakeLlmClient(["just text"])
    result = await fake_router(fake).complete("note.extract", system="s", user_text="u")
    assert result.parsed is None
    assert len(fake.calls) == 1


async def test_model_recommended_sampling_is_applied_with_no_override() -> None:
    # The whole-catalog fix: even when the caller passes NO sampling, the resolved model's
    # recommended defaults reach the client — a local Qwen VL runs at its card values, not
    # llama.cpp's engine defaults.
    fake = FakeLlmClient(["ok"])
    router = LlmRouter({"local": fake}, {"vision.caption": ("local", "qwen3-vl-30b-a3b")})
    await router.complete("vision.caption", system="s", user_text="u")
    sampling = fake.calls[0]["sampling"]
    assert sampling.temperature == 0.7 and sampling.top_p == 0.8
    assert sampling.top_k == 20 and sampling.presence_penalty == 1.5


async def test_cloud_default_sampling_reaches_the_client() -> None:
    fake = FakeLlmClient(["ok"])
    router = LlmRouter({"xai": fake}, {"note.extract": ("xai", "grok-4.3")})
    await router.complete("note.extract", system="s", user_text="u")
    assert fake.calls[0]["sampling"] == Sampling(temperature=0.7, top_p=0.95)


async def test_per_task_override_merges_over_the_model_default() -> None:
    # The OCR case: a near-greedy override (temperature + presence_penalty) merges ON TOP of
    # the model's recommended defaults, so top_p/top_k stay the card's while temperature drops.
    fake = FakeLlmClient(["ok"])
    router = LlmRouter({"local": fake}, {"vision.ocr": ("local", "qwen3-vl-30b-a3b")})
    await router.complete(
        "vision.ocr",
        system="s",
        user_text="u",
        sampling=Sampling(temperature=0.1, presence_penalty=1.5),
    )
    s = fake.calls[0]["sampling"]
    assert s.temperature == 0.1  # override won
    assert s.top_p == 0.8 and s.top_k == 20  # model default preserved
    assert s.presence_penalty == 1.5


async def test_converse_threads_resolved_sampling() -> None:
    fake = FakeLlmClient()
    router = LlmRouter({"local": fake}, {"agent.turn": ("local", "qwen3.5-4b")})
    await router.converse("agent.turn", system="s", messages=[])
    # qwen3.5-4b is hybrid; agent.turn is the medium bucket (reasoning_effort None → thinking
    # on for a hybrid), so the thinking-mode row is what reaches the client.
    assert fake.converse_calls[0]["sampling"].temperature == 1.0


async def test_build_router_wires_all_three_providers() -> None:
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.anthropic.com":
            assert request.headers["x-api-key"] == "ant-key"
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "from-anthropic"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        if request.url.host == "api.x.ai":
            assert request.headers["authorization"] == "Bearer xai-key"
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": f"from-{body['model']}"}}]}
        )

    settings = Settings(
        anthropic_api_key="ant-key",
        xai_api_key="xai-key",
        llm_tasks={
            "note.extract": "anthropic:claude-sonnet-4-6",
            "vision.ocr": "local:llava",
        },
    )

    async def no_sleep(seconds: float) -> None:  # injected so retries never wait in tests
        raise AssertionError("no retry expected")

    router = build_router(
        settings,
        transport=httpx.MockTransport(handle),
        sleep=no_sleep,
        residency=_inert_residency(),
    )

    assert (
        await router.complete("note.extract", system="s", user_text="u")
    ).text == "from-anthropic"
    assert (await router.complete("vision.ocr", system="s", user_text="u")).text == "from-llava"
    # Untouched tasks keep the xai:grok-4.3 default.
    assert (
        await router.complete("fact.adjudicate", system="s", user_text="u")
    ).text == "from-grok-4.3"
    assert hosts == ["api.anthropic.com", "localhost", "api.x.ai"]


# --- capability-tier (model strength) resolution -----------------------------


def _tiered_router(
    xai: FakeLlmClient, anthropic: FakeLlmClient, *, pinned=frozenset()
) -> LlmRouter:
    return LlmRouter(
        {"xai": xai, "anthropic": anthropic},
        {"note.extract": ("xai", "grok-4.3")},
        tiers={"high": ("anthropic", "claude-x"), "low": ("xai", "grok-cheap")},
        pinned=pinned,
    )


async def test_strength_resolves_through_the_tier_not_the_task_default() -> None:
    xai, anthropic = FakeLlmClient(["x"]), FakeLlmClient(["a"])
    router = _tiered_router(xai, anthropic)
    await router.complete("note.extract", system="s", user_text="u", strength="high")
    # high tier -> anthropic:claude-x, overriding the task default xai:grok-4.3.
    assert anthropic.calls[0]["model"] == "claude-x" and not xai.calls
    assert router.spec("note.extract", "high") == ("anthropic", "claude-x")


async def test_explicit_task_pin_outranks_the_prompt_strength() -> None:
    xai, anthropic = FakeLlmClient(["x"]), FakeLlmClient(["a"])
    router = _tiered_router(xai, anthropic, pinned=frozenset({"note.extract"}))
    await router.complete("note.extract", system="s", user_text="u", strength="high")
    # The operator pinned the task, so the pin wins over the prompt's tier.
    assert xai.calls[0]["model"] == "grok-4.3" and not anthropic.calls


async def test_unknown_strength_tier_raises() -> None:
    router = _tiered_router(FakeLlmClient(), FakeLlmClient())
    with pytest.raises(LlmError, match="unknown LLM strength tier"):
        await router.complete("note.extract", system="s", user_text="u", strength="turbo")


def test_resolve_tiers_defaults_overrides_and_unknown() -> None:
    from jbrain.llm.router import TIER_DEFAULTS, resolve_tiers

    assert resolve_tiers({})["high"] == ("xai", "grok-4.3")
    assert set(resolve_tiers({})) == set(TIER_DEFAULTS)
    assert resolve_tiers({"high": "anthropic:claude-sonnet-4-6"})["high"] == (
        "anthropic",
        "claude-sonnet-4-6",
    )
    with pytest.raises(LlmError, match="unknown LLM tier"):
        resolve_tiers({"genius": "xai:x"})


def test_build_router_marks_pinned_tasks_so_pins_beat_tiers() -> None:
    router = build_router(
        Settings(llm_tasks={"note.extract": "anthropic:claude-sonnet-4-6"}),
        residency=_inert_residency(),
    )
    # The pinned task resolves to its pin even when a strength tier is requested.
    assert router.spec("note.extract", "high") == ("anthropic", "claude-sonnet-4-6")
    # An unpinned task still honours the tier.
    assert router.spec("vision.ocr", "high") == ("xai", "grok-4.3")


def test_build_router_requires_a_residency_admitter() -> None:
    """The core invariant, now enforced by the signature rather than by a fallback.

    The gateway never self-evicts (`swap: false`), so an unadmitted local load hard-locks
    the unified-memory box — the worker OOM was exactly a router built without admission.
    build_router used to paper over that with a `_default_residency` built from settings.
    That default was the WEAKER of two gates: no `hold_loader`, so `_held_names()` returned
    an empty set, and empty means *not held* — admit everything. An operator reservation
    could be live on the api's coordinator and invisible on this one. No production caller
    ever used it (`main.py` and `worker.py` both pass their own), so it existed only to be
    silently wrong for whoever forgot. It is gone; the parameter is required."""
    from jbrain.llm.residency import ResidencyCoordinator

    with pytest.raises(TypeError):
        build_router(Settings(local_llm_enabled=True))  # type: ignore[call-arg]

    # A caller that owns a coordinator gets THAT instance (shared bookkeeping).
    mine = ResidencyCoordinator(object(), enabled=True)  # type: ignore[arg-type]
    assert build_router(Settings(local_llm_enabled=True), residency=mine)._residency is mine


# --- live DB overrides (the settings screen) ---------------------------------


def _loader(overrides: dict[str, dict[str, str]]):  # type: ignore[no-untyped-def]
    async def load() -> dict[str, dict[str, str]]:
        return overrides

    return load


def _override_router(
    clients: dict[str, FakeLlmClient], overrides: dict[str, dict[str, str]], *, pinned=frozenset()
) -> LlmRouter:
    async def load() -> dict[str, dict[str, str]]:
        return overrides

    return LlmRouter(
        clients,
        {"note.extract": ("xai", "grok-4.3")},
        tiers={"high": ("xai", "grok-strong"), "low": ("xai", "grok-cheap")},
        pinned=pinned,
        overrides_loader=load,
    )


async def test_stored_spec_overrides_env_pin_and_tier() -> None:
    xai, anthropic = FakeLlmClient(["x"]), FakeLlmClient(["a"])
    # Task is env-pinned AND a strength tier is requested; the stored spec must
    # win over both — the UI is the live control surface.
    router = _override_router(
        {"xai": xai, "anthropic": anthropic},
        {"note.extract": {"spec": "anthropic:claude-x"}},
        pinned=frozenset({"note.extract"}),
    )
    await router.complete("note.extract", system="s", user_text="u", strength="high")
    assert anthropic.calls[0]["model"] == "claude-x" and not xai.calls


async def test_stale_local_override_ignored_when_hosting_disabled() -> None:
    # A `local:` spec saved while hosting was on, then disabled: the call must
    # fall back to the cloud default rather than route at a dead gateway.
    xai, local = FakeLlmClient(["x"]), FakeLlmClient(["l"])

    async def load() -> dict[str, dict[str, str]]:
        return {"note.extract": {"spec": "local:qwen3-vl-30b-a3b"}}

    router = LlmRouter(
        {"xai": xai, "local": local},
        {"note.extract": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_enabled=False,
    )
    await router.complete("note.extract", system="s", user_text="u")
    assert xai.calls and not local.calls

    # With hosting enabled the same override IS honored.
    xai2, local2 = FakeLlmClient(["x"]), FakeLlmClient(["l"])
    router2 = LlmRouter(
        {"xai": xai2, "local": local2},
        {"note.extract": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_enabled=True,
    )
    await router2.complete("note.extract", system="s", user_text="u")
    assert local2.calls and not xai2.calls


async def test_primary_local_served_model_returns_the_model_when_agent_turn_is_local() -> None:
    # The WarmKeeper reads this to decide which model to keep hot: a live local override on
    # agent.turn yields its served-model name.
    async def load() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"spec": "local:gpt-oss-120b"}}

    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_enabled=True,
    )
    assert await router.primary_local_served_model() == "gpt-oss-120b"


async def test_primary_local_served_model_is_none_for_a_cloud_route() -> None:
    # agent.turn on its cloud default — nothing to keep resident on the box.
    router = LlmRouter({"xai": FakeLlmClient()}, {"agent.turn": ("xai", "grok-4.3")})
    assert await router.primary_local_served_model() is None


async def test_primary_local_served_model_is_none_when_hosting_disabled() -> None:
    # A stale local override with hosting off degrades to the cloud default → None, so the
    # keeper never loads at a dead gateway.
    async def load() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"spec": "local:gpt-oss-120b"}}

    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_enabled=False,
    )
    assert await router.primary_local_served_model() is None


async def test_context_window_for_a_cloud_model_reads_the_table() -> None:
    # A task resolving to a known cloud model reports that model's window (the
    # meter's denominator); grok-4.3 is in the table.
    router = LlmRouter({"xai": FakeLlmClient()}, {"agent.turn": ("xai", "grok-4.3")})
    assert await router.context_window("agent.turn") == CONTEXT_WINDOWS["grok-4.3"]


async def test_supports_vision_is_true_for_a_cloud_route() -> None:
    # The wired cloud providers (Grok, Claude) are multimodal, so a non-local route
    # keeps image bytes inline.
    router = LlmRouter({"xai": FakeLlmClient()}, {"agent.turn": ("xai", "grok-4.3")})
    assert await router.supports_vision("agent.turn") is True


async def test_supports_vision_reflects_the_local_catalog() -> None:
    # A live local override decides vision by the catalog: the VL model can see,
    # the text-only gpt-oss cannot (its bytes must be dropped).
    async def vl() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"spec": "local:qwen3-vl-30b-a3b"}}

    async def oss() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"spec": "local:gpt-oss-120b"}}

    def _router(load) -> LlmRouter:  # type: ignore[no-untyped-def]
        return LlmRouter(
            {"xai": FakeLlmClient(), "local": FakeLlmClient()},
            {"agent.turn": ("xai", "grok-4.3")},
            overrides_loader=load,
            local_enabled=True,
        )

    assert await _router(vl).supports_vision("agent.turn") is True
    assert await _router(oss).supports_vision("agent.turn") is False


async def test_context_window_falls_back_for_an_unlisted_model() -> None:
    # An unlisted model degrades to the conservative default rather than misreport.
    router = LlmRouter({"anthropic": FakeLlmClient()}, {"agent.turn": ("anthropic", "claude-x")})
    assert await router.context_window("agent.turn") == DEFAULT_CONTEXT_WINDOW


async def test_context_window_for_a_local_model_reads_the_catalog() -> None:
    # A live local override points the meter at the gateway's `-c` from the catalog.
    async def load() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"spec": "local:gpt-oss-120b"}}

    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_enabled=True,
    )
    assert await router.context_window("agent.turn") == 131072


async def test_context_window_honors_a_per_model_override() -> None:
    # A per-model window override (catalog id → tokens) wins over the catalog
    # default for the meter; an absent override falls back to the catalog.
    async def load() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"spec": "local:gpt-oss-120b"}}

    async def windows() -> dict[str, int]:
        return {"gpt-oss-120b": 65536}

    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_windows_loader=windows,
        local_enabled=True,
    )
    assert await router.context_window("agent.turn") == 65536

    async def empty_windows() -> dict[str, int]:
        return {}

    fallback = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=load,
        local_windows_loader=empty_windows,
        local_enabled=True,
    )
    assert await fallback.context_window("agent.turn") == 131072


async def test_stored_reasoning_effort_reaches_xai_client() -> None:
    xai = FakeLlmClient(["x"])
    router = _override_router({"xai": xai}, {"note.extract": {"reasoning_effort": "high"}})
    await router.complete("note.extract", system="s", user_text="u")
    assert xai.calls[0]["reasoning_effort"] == "high"


async def test_reasoning_effort_dropped_when_override_routes_off_xai() -> None:
    xai, anthropic = FakeLlmClient(["x"]), FakeLlmClient(["a"])
    # A stored effort is meaningless once the spec routes to anthropic.
    router = _override_router(
        {"xai": xai, "anthropic": anthropic},
        {"note.extract": {"spec": "anthropic:claude-x", "reasoning_effort": "high"}},
    )
    await router.complete("note.extract", system="s", user_text="u")
    assert anthropic.calls[0]["reasoning_effort"] is None


async def test_effective_reasoning_effort_reports_the_live_effort() -> None:
    # The accessor the agent loop uses to size its budget: the stored effort for a
    # reasoning-capable task (xai default), None once the task routes off a reasoning
    # model (Claude has no effort channel).
    xai, anthropic = FakeLlmClient(["x"]), FakeLlmClient(["a"])
    on = _override_router({"xai": xai}, {"note.extract": {"reasoning_effort": "high"}})
    assert await on.effective_reasoning_effort("note.extract") == "high"
    off = _override_router(
        {"xai": xai, "anthropic": anthropic},
        {"note.extract": {"spec": "anthropic:claude-x", "reasoning_effort": "high"}},
    )
    assert await off.effective_reasoning_effort("note.extract") is None


async def test_effective_spec_follows_a_live_override_unlike_spec() -> None:
    # Provenance regression: `spec()` sees only static config, so it reported the
    # default route (and stamped the wrong model) even after the operator re-routed a
    # task in Settings. `effective_spec()` folds the live override in, matching the
    # model `complete` actually calls.
    xai, local = FakeLlmClient(["x"]), FakeLlmClient(["l"])
    router = LlmRouter(
        {"xai": xai, "local": local},
        {"vision.ocr": ("xai", "grok-4.3")},
        overrides_loader=_loader({"vision.ocr": {"spec": "local:qwen3-vl-30b-a3b"}}),
        local_enabled=True,
    )
    assert router.spec("vision.ocr") == ("xai", "grok-4.3")  # static default, override-blind
    assert await router.effective_spec("vision.ocr") == ("local", "qwen3-vl-30b-a3b")


async def test_effective_spec_ignores_an_unservable_local_override() -> None:
    # Same fail-safe as the call path: a stored `local:` route is dropped when local
    # hosting is off, so the stamp falls back to the resolvable default rather than
    # naming a model that can't run.
    xai = FakeLlmClient(["x"])
    router = LlmRouter(
        {"xai": xai},
        {"vision.ocr": ("xai", "grok-4.3")},
        overrides_loader=_loader({"vision.ocr": {"spec": "local:qwen3-vl-30b-a3b"}}),
        local_enabled=False,
    )
    assert await router.effective_spec("vision.ocr") == ("xai", "grok-4.3")


async def test_reasoning_effort_reaches_a_reasoning_capable_local_model() -> None:
    # A stored effort on a `local:` spec for a reasoning model (gpt-oss) is honored —
    # llama.cpp serves gpt-oss with a harmony reasoning channel.
    local = FakeLlmClient(["l"])
    router = LlmRouter(
        {"local": local},
        {"note.extract": ("xai", "grok-4.3")},
        overrides_loader=_loader(
            {"note.extract": {"spec": "local:gpt-oss-120b", "reasoning_effort": "high"}}
        ),
        local_enabled=True,
    )
    await router.complete("note.extract", system="s", user_text="u")
    assert local.calls[0]["reasoning_effort"] == "high"


async def test_reasoning_effort_dropped_for_a_non_reasoning_local_model() -> None:
    # The same stored effort on a non-reasoning local model (a Qwen Instruct variant)
    # is dropped — it would be meaningless (no thinking channel) on the wire.
    local = FakeLlmClient(["l"])
    router = LlmRouter(
        {"local": local},
        {"note.extract": ("xai", "grok-4.3")},
        overrides_loader=_loader(
            {"note.extract": {"spec": "local:qwen3-30b-a3b", "reasoning_effort": "high"}}
        ),
        local_enabled=True,
    )
    await router.complete("note.extract", system="s", user_text="u")
    assert local.calls[0]["reasoning_effort"] is None


async def test_reasoning_effort_reaches_a_hybrid_qwen_local_model() -> None:
    # A Qwen hybrid (qwen3.5-*) is reasoning-capable, so a stored effort — including
    # "none" — now reaches the client, which translates it to the enable_thinking
    # toggle. The router's job is only to stop dropping it; the client owns the mapping.
    local = FakeLlmClient(["l"])
    router = LlmRouter(
        {"local": local},
        {"research.title": ("xai", "grok-4.3")},
        overrides_loader=_loader(
            {"research.title": {"spec": "local:qwen3.5-0.8b", "reasoning_effort": "none"}}
        ),
        local_enabled=True,
    )
    await router.complete("research.title", system="s", user_text="u")
    assert local.calls[0]["reasoning_effort"] == "none"


async def test_bucket_default_effort_sent_without_an_override() -> None:
    # Right-by-default: a high-bucket task (integrate.note) reaches the client at
    # high with no stored override; a medium-bucket task (agent.turn) sends None —
    # the model's own default — so the sub-agent spawner's "no chosen effort → the
    # child model's default" contract still holds.
    xai = FakeLlmClient(["a", "b"])
    router = LlmRouter(
        {"xai": xai},
        {"integrate.note": ("xai", "grok-4.3"), "agent.turn": ("xai", "grok-4.3")},
    )
    await router.complete("integrate.note", system="s", user_text="u")
    assert xai.calls[0]["reasoning_effort"] == "high"
    await router.complete("agent.turn", system="s", user_text="u")
    assert xai.calls[1]["reasoning_effort"] is None


async def test_low_bucket_default_effort_sent_without_an_override() -> None:
    # research.title, not session.title: naming a CHAT is no longer a routed completion at all
    # (jerv calls `name_session` mid-turn), so the low bucket's remaining title task is the
    # deep-research report heading.
    xai = FakeLlmClient(["a"])
    router = LlmRouter({"xai": xai}, {"research.title": ("xai", "grok-4.3")})
    await router.complete("research.title", system="s", user_text="u")
    assert xai.calls[0]["reasoning_effort"] == "low"


async def test_stored_effort_override_wins_over_the_bucket_default() -> None:
    # integrate.note defaults high; a stored 'low' override must win over it.
    xai = FakeLlmClient(["a"])
    router = LlmRouter(
        {"xai": xai},
        {"integrate.note": ("xai", "grok-4.3")},
        overrides_loader=_loader({"integrate.note": {"reasoning_effort": "low"}}),
    )
    await router.complete("integrate.note", system="s", user_text="u")
    assert xai.calls[0]["reasoning_effort"] == "low"


async def test_converse_effort_override_wins_for_a_reasoning_model() -> None:
    # The per-call override (the sub-agent spawner's per-child effort) beats the
    # stored/default effort when the resolved model is reasoning-capable (xai Grok).
    xai = FakeLlmClient(["x"])
    router = _override_router({"xai": xai}, {"note.extract": {"reasoning_effort": "low"}})
    await router.converse("note.extract", system="s", messages=[], effort_override="high")
    assert xai.converse_calls[0]["reasoning_effort"] == "high"


async def test_converse_effort_override_dropped_for_a_non_reasoning_model() -> None:
    # An override aimed at a non-reasoning route (Claude has no effort channel) is
    # dropped, exactly like a stored effort — the param never reaches the wire.
    anthropic = FakeLlmClient(["a"])
    router = _override_router(
        {"anthropic": anthropic}, {"note.extract": {"spec": "anthropic:claude-x"}}
    )
    await router.converse("note.extract", system="s", messages=[], effort_override="high")
    assert anthropic.converse_calls[0]["reasoning_effort"] is None


async def test_converse_spec_override_wins_over_the_resolved_route() -> None:
    # The omnibox's per-conversation model pick: a per-call spec_override steers this
    # turn onto the picked model, outranking even a stored spec.
    xai, local = FakeLlmClient(["x"]), FakeLlmClient(["l"])
    router = LlmRouter(
        {"xai": xai, "local": local},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=_loader({"agent.turn": {"spec": "xai:grok-4.3"}}),
        local_enabled=True,
    )
    await router.converse("agent.turn", system="s", messages=[], spec_override="local:gpt-oss-120b")
    assert local.converse_calls[0]["model"] == "gpt-oss-120b" and not xai.converse_calls


async def test_converse_stream_spec_override_steers_the_model() -> None:
    xai, local = FakeLlmClient(["x"]), FakeLlmClient(["l"])
    router = LlmRouter(
        {"xai": xai, "local": local},
        {"agent.turn": ("xai", "grok-4.3")},
        local_enabled=True,
    )
    async for _ in router.converse_stream(
        "agent.turn", system="s", messages=[], spec_override="local:gpt-oss-120b"
    ):
        pass
    assert local.stream_calls[0]["model"] == "gpt-oss-120b" and not xai.stream_calls


async def test_spec_override_re_gates_reasoning_effort_on_the_picked_model() -> None:
    # Steering onto a non-reasoning local model drops the effort the resolved (xai)
    # route would have carried — the param never reaches a model with no thinking channel.
    local = FakeLlmClient(["l"])
    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": local},
        {"agent.turn": ("xai", "grok-4.3")},
        overrides_loader=_loader({"agent.turn": {"reasoning_effort": "high"}}),
        local_enabled=True,
    )
    await router.converse(
        "agent.turn", system="s", messages=[], spec_override="local:qwen3-30b-a3b"
    )
    assert local.converse_calls[0]["reasoning_effort"] is None


async def test_spec_override_ignored_when_local_hosting_off() -> None:
    # A local pick can't route at a dead gateway: the override is dropped and the turn
    # runs on the resolved cloud default (the same fail-safe as a stored local spec).
    xai, local = FakeLlmClient(["x"]), FakeLlmClient(["l"])
    router = LlmRouter(
        {"xai": xai, "local": local},
        {"agent.turn": ("xai", "grok-4.3")},
        local_enabled=False,
    )
    await router.converse("agent.turn", system="s", messages=[], spec_override="local:gpt-oss-120b")
    assert xai.converse_calls and not local.converse_calls


async def test_spec_override_reflects_in_window_and_vision_probes() -> None:
    # The endpoint probes the picked model's window + vision, not the default route's:
    # the local VL model reports its catalog window and vision-capable.
    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3")},
        local_enabled=True,
    )
    vl = "local:qwen3-vl-30b-a3b"
    assert await router.context_window("agent.turn", spec_override=vl) == 32768
    assert await router.supports_vision("agent.turn", spec_override=vl) is True
    # ...and without the override it stays on the cloud default.
    assert await router.context_window("agent.turn") == CONTEXT_WINDOWS["grok-4.3"]


async def test_bad_stored_spec_falls_back_without_crashing() -> None:
    xai = FakeLlmClient(["x"])
    router = _override_router({"xai": xai}, {"note.extract": {"spec": "garbage"}})
    result = await router.complete("note.extract", system="s", user_text="u")
    # Malformed spec ignored; the call still succeeds on the resolved default.
    assert result.text == "x" and xai.calls[0]["model"] == "grok-4.3"


async def test_no_loader_keeps_legacy_behavior() -> None:
    xai = FakeLlmClient(["x"])
    router = LlmRouter({"xai": xai}, {"note.extract": ("xai", "grok-4.3")})
    await router.complete("note.extract", system="s", user_text="u")
    assert xai.calls[0]["reasoning_effort"] is None


async def test_converse_threads_stored_reasoning_effort() -> None:
    xai = FakeLlmClient(["x"])
    router = _override_router({"xai": xai}, {"note.extract": {"reasoning_effort": "low"}})
    await router.converse("note.extract", system="s", messages=[])
    assert xai.converse_calls[0]["reasoning_effort"] == "low"


async def test_converse_stream_threads_stored_reasoning_effort() -> None:
    xai = FakeLlmClient(["x"])
    router = _override_router({"xai": xai}, {"note.extract": {"reasoning_effort": "medium"}})
    async for _ in router.converse_stream("note.extract", system="s", messages=[]):
        pass
    assert xai.stream_calls[0]["reasoning_effort"] == "medium"


def test_toks_per_s_is_end_to_end_throughput() -> None:
    # The per-call t/s logged with every llm.complete/converse: output tokens over
    # the wall-clock interval (prefill included), guarding against divide-by-zero.
    assert LlmRouter._toks_per_s(120, 4.0) == 30.0
    assert LlmRouter._toks_per_s(0, 2.0) == 0.0
    assert LlmRouter._toks_per_s(120, 0.0) is None
    assert LlmRouter._toks_per_s(50, -1.0) is None


# --- titles follow the primary chat model (agent.turn) -----------------------


def _title_router(clients, overrides, *, pinned=frozenset()):  # type: ignore[no-untyped-def]
    async def load():  # type: ignore[no-untyped-def]
        return overrides

    return LlmRouter(
        clients,
        {
            "agent.turn": ("xai", "grok-4.3"),
            "research.title": ("xai", "grok-4.3"),
        },
        tiers={"high": ("xai", "grok-strong"), "low": ("xai", "grok-cheap")},
        pinned=pinned,
        overrides_loader=load,
        local_enabled=True,
    )


async def test_title_follows_a_re_routed_agent_model() -> None:
    # agent.turn re-routed to a local model; a title with no override of its own moves with it
    # (the fix: no separate title override to remember on a local-only box).
    router = _title_router(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": {"spec": "local:gpt-oss-120b"}},
    )
    assert await router.effective_spec("research.title") == ("local", "gpt-oss-120b")


async def test_title_default_unchanged_without_an_agent_override() -> None:
    # No overrides at all: a fresh box still runs the title on the shipped default.
    router = _title_router({"xai": FakeLlmClient()}, {})
    assert await router.effective_spec("research.title") == ("xai", "grok-4.3")


async def test_a_stale_title_pin_is_ignored_and_it_follows_agent_turn() -> None:
    # The title task is NOT independently routable: a stored own-task spec (a stale pin from
    # before the picker hid it, or one set via a direct PUT) is IGNORED, and the title follows
    # agent.turn's model. Otherwise naming a report would swap in the pinned model — the exact
    # "titling swaps in a different model" problem the follow prevents.
    router = _title_router(
        {"xai": FakeLlmClient(), "local": FakeLlmClient(), "anthropic": FakeLlmClient()},
        {
            "agent.turn": {"spec": "local:gpt-oss-120b"},
            "research.title": {"spec": "anthropic:claude-x"},  # stale own-task pin — ignored
        },
    )
    assert await router.effective_spec("research.title") == ("local", "gpt-oss-120b")


async def test_title_keeps_its_own_low_effort_when_following() -> None:
    # Following agent.turn's model, the title keeps its OWN low effort — not agent.turn's medium.
    router = _title_router(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": {"spec": "local:gpt-oss-120b"}},
    )
    assert await router.effective_reasoning_effort("research.title") == "low"


async def test_title_with_a_strength_tier_opts_out_of_the_follow() -> None:
    # A caller that passes a strength tier resolves by tier, not the followed agent model.
    router = _title_router(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": {"spec": "local:gpt-oss-120b"}},
    )
    assert await router.effective_spec("research.title", "low") == ("xai", "grok-cheap")


async def test_non_title_task_does_not_follow_the_agent_model() -> None:
    # A normal task keeps its own routing; only the title tasks follow agent.turn.
    router = LlmRouter(
        {"xai": FakeLlmClient(), "local": FakeLlmClient()},
        {"agent.turn": ("xai", "grok-4.3"), "note.extract": ("xai", "grok-4.3")},
        overrides_loader=_loader({"agent.turn": {"spec": "local:gpt-oss-120b"}}),
        local_enabled=True,
    )
    assert await router.effective_spec("note.extract") == ("xai", "grok-4.3")


async def test_admit_local_load_reaches_residency_admission() -> None:
    """The real method, not a fake's stand-in.

    Its whole justification is that a caller which loads a model itself goes through the SAME
    admission a routed completion does, rather than reaching into `_admit_local` — which would
    be the same bypass with extra steps. Every warm-keeper test substitutes its own router, so
    without this nothing proved the wiring existed at all."""

    class _Admitter:
        def __init__(self) -> None:
            self.rooms: list[str] = []

        async def ensure_room(self, served_model: str) -> None:
            self.rooms.append(served_model)

    admitter = _Admitter()
    r = LlmRouter({}, {}, residency=admitter)
    await r.admit_local_load("qwen3.8-27b-q4")
    assert admitter.rooms == ["qwen3.8-27b-q4"]


async def test_admit_local_load_is_inert_without_a_coordinator() -> None:
    """A cloud-only box holds no coordinator. Admission is then a no-op rather than an error —
    the keeper still loads, it just has nothing to make room against."""
    r = LlmRouter({}, {}, residency=None)
    await r.admit_local_load("qwen3.8-27b-q4")  # must not raise


class _SlowClient:
    """A client that keeps the caller waiting before it streams — a local turn in prefill."""

    def __init__(self, wait_s: float) -> None:
        self._wait_s = wait_s

    async def converse_stream(self, **_kwargs: object):  # noqa: ANN003 — structural fake
        await asyncio.sleep(self._wait_s)
        yield LlmTurn(text="ok", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))


async def _drain(router: LlmRouter, **kwargs: object) -> None:
    async for _ in router.converse_stream("agent.turn", system="s", messages=[], **kwargs):  # type: ignore[arg-type]
        pass


async def test_a_local_turn_stuck_in_prefill_is_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The instrument this exists for: the box's longest unread silence is the gap before a
    # local model's first token, and `/slots` is the only thing that can describe it.
    monkeypatch.setattr("jbrain.llm.prefill._FIRST_DELAY_S", 0.02)
    probed: list[str] = []

    async def slots(served_model: str) -> list[dict[str, object]]:
        probed.append(served_model)
        return [{"id": 0}]

    router = LlmRouter(
        {"local": cast(LlmClient, _SlowClient(0.1))},
        {"agent.turn": ("local", "gpt-oss-120b")},
        slots_probe=slots,
    )
    await _drain(router)
    assert probed == ["gpt-oss-120b"]


async def test_a_cloud_turn_is_never_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    # `/slots` belongs to llama-server. A slow Claude turn is slow for reasons no endpoint on
    # this box can see, and probing one would ask the local gateway about a model it isn't
    # serving — reaching a passthrough that LOADS on demand, outside residency.
    monkeypatch.setattr("jbrain.llm.prefill._FIRST_DELAY_S", 0.02)
    probed: list[str] = []

    async def slots(served_model: str) -> list[dict[str, object]]:
        probed.append(served_model)
        return []

    router = LlmRouter(
        {"anthropic": cast(LlmClient, _SlowClient(0.1))},
        {"agent.turn": ("anthropic", "claude-sonnet-4-5")},
        slots_probe=slots,
    )
    await _drain(router)
    assert probed == []


async def test_a_fast_local_turn_costs_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jbrain.llm.prefill._FIRST_DELAY_S", 0.2)
    probed: list[str] = []

    async def slots(served_model: str) -> list[dict[str, object]]:
        probed.append(served_model)
        return []

    router = LlmRouter(
        {"local": FakeLlmClient(["quick"])},
        {"agent.turn": ("local", "gpt-oss-120b")},
        slots_probe=slots,
    )
    await _drain(router)
    await asyncio.sleep(0.3)
    assert probed == [], "the watch must not outlive the turn"
