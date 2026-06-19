"""Router behavior: task resolution, the JSON re-ask, and build_router wiring."""

import json

import httpx
import pytest

from jbrain.config import Settings
from jbrain.llm import (
    FakeLlmClient,
    LlmBadResponseError,
    LlmError,
    LlmRouter,
    build_router,
)
from jbrain.llm.router import JSON_NUDGE

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def fake_router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter({"xai": fake}, {"note.extract": ("xai", "grok-4.3")})


async def test_complete_routes_task_to_provider_model() -> None:
    fake = FakeLlmClient(["fine"])
    result = await fake_router(fake).complete("note.extract", system="s", user_text="u")
    assert result.text == "fine"
    assert fake.calls[0]["model"] == "grok-4.3"
    assert fake.calls[0]["system"] == "s"


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

    router = build_router(settings, transport=httpx.MockTransport(handle), sleep=no_sleep)

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
    router = build_router(Settings(llm_tasks={"note.extract": "anthropic:claude-sonnet-4-6"}))
    # The pinned task resolves to its pin even when a strength tier is requested.
    assert router.spec("note.extract", "high") == ("anthropic", "claude-sonnet-4-6")
    # An unpinned task still honours the tier.
    assert router.spec("vision.ocr", "high") == ("xai", "grok-4.3")


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
