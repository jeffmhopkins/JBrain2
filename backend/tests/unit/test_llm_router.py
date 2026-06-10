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
