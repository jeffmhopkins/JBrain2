"""The conversation cache's wiring outside the store: the cached-token count from the real
OpenAI-compatible adapter (both shapes), and the owner/debug switch that turns the cache off and
deletes what it saved (FLASH_NEXT F4c)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from jbrain.api import llm_settings
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.types import LlmTurn, UserMessage
from tests.unit.fakes import FakeSettingsStore


def _client(transport: httpx.MockTransport) -> OpenAiCompatClient:
    return OpenAiCompatClient(
        "http://localhost:11434/v1", "", provider="local", transport=transport
    )


async def test_a_turn_carries_the_servers_cached_prompt_tokens() -> None:
    body = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 40_000,
            "completion_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 39_800},
        },
    }
    client = _client(httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))
    turn = await client.converse(model="m", system="s", messages=[UserMessage(text="u")])
    assert (turn.usage.input_tokens, turn.usage.cached_tokens) == (40_000, 39_800)
    result = await client.complete(model="m", system="s", user_text="u")
    assert result.usage.cached_tokens == 39_800


async def test_a_streamed_turn_carries_them_too_and_absent_reads_as_zero() -> None:
    def stream(usage: dict[str, Any]) -> bytes:
        events = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage},
        ]
        return (
            "\n\n".join(f"data: {json.dumps(e)}" for e in events) + "\n\ndata: [DONE]\n\n"
        ).encode()

    async def last(body: bytes) -> LlmTurn:
        client = _client(
            httpx.MockTransport(
                lambda _r: httpx.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            )
        )
        parts = [
            p
            async for p in client.converse_stream(
                model="m", system="s", messages=[UserMessage(text="u")]
            )
        ]
        assert isinstance(parts[-1], LlmTurn)
        return parts[-1]

    cached = await last(
        stream(
            {
                "prompt_tokens": 9,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 7},
            }
        )
    )
    assert cached.usage.cached_tokens == 7
    bare = await last(stream({"prompt_tokens": 9, "completion_tokens": 1}))
    assert bare.usage.cached_tokens == 0


async def test_llama_servers_own_timings_stand_in_when_usage_carries_no_cached_count() -> None:
    # A build whose usage block omits `cached_tokens` still reports `timings.cache_n`: the
    # reuse diagnostic (`turn_reuse`) must not read every follow-up as a full re-read.
    timings = {"cache_n": 117_000, "prompt_n": 512}
    body = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 117_512, "completion_tokens": 3},
        "timings": timings,
    }
    client = _client(httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))
    turn = await client.converse(model="m", system="s", messages=[UserMessage(text="u")])
    assert turn.usage.cached_tokens == 117_000

    events = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": timings},
        {"choices": [], "usage": {"prompt_tokens": 117_512, "completion_tokens": 1}},
    ]
    sse = ("\n\n".join(f"data: {json.dumps(e)}" for e in events) + "\n\ndata: [DONE]\n\n").encode()
    client = _client(
        httpx.MockTransport(
            lambda _r: httpx.Response(
                200, content=sse, headers={"content-type": "text/event-stream"}
            )
        )
    )
    parts = [
        p async for p in client.converse_stream(model="m", system="s", messages=[UserMessage("u")])
    ]
    assert isinstance(parts[-1], LlmTurn) and parts[-1].usage.cached_tokens == 117_000


class _Store:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def configure(self, **kw: object) -> None:
        self.calls.append(kw)

    async def clear_conversations(self) -> int:
        self.calls.append("cleared")
        return 3


async def test_turning_the_cache_off_deletes_every_conversation_file() -> None:
    settings = FakeSettingsStore()
    store = _Store()
    out = await llm_settings.set_kv_conversation_cache(
        settings,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        enabled=False,
        kv_prefix=store,  # type: ignore[arg-type]
    )
    assert out["conversation_cache"] is False and out["files_removed"] == 3
    assert store.calls == [{"conversations": False}, "cleared"]
    assert settings.values["llm_kv_conversation_cache"] is False
    on = await llm_settings.set_kv_conversation_cache(
        settings,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        enabled=True,
        kv_prefix=store,  # type: ignore[arg-type]
    )
    assert on["files_removed"] == 0 and store.calls[-1] == {"conversations": True}


async def test_a_malformed_stored_toggle_reads_as_off_and_unset_as_on() -> None:
    from jbrain.settings_store import SqlSettingsStore

    class _Rows(SqlSettingsStore):
        def __init__(self, value: object) -> None:
            self._value = value

        async def get(self, ctx: Any, key: str, default: object = None) -> object:
            return default if self._value is _UNSET else self._value

    _UNSET = object()
    assert await _Rows(_UNSET).llm_kv_conversation_cache(None) is True  # type: ignore[arg-type]
    assert await _Rows(True).llm_kv_conversation_cache(None) is True  # type: ignore[arg-type]
    for junk in (False, "yes", 1, None, {"on": True}):
        assert await _Rows(junk).llm_kv_conversation_cache(None) is False  # type: ignore[arg-type]
