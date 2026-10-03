"""The router's half of slot pinning on a pooled model (FLASH_NEXT_ENGINE_PLAN §4a): which
slot a call carries, its cap, the layout fallback, and that nothing else ever carries one."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest

from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, prefill
from jbrain.llm import engine as engines
from jbrain.llm.errors import LlmStreamTruncatedError
from jbrain.llm.kv_pool_guard import KvPoolGuard
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.router import context_window_for_spec
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, SlotCapError, SlotRole
from jbrain.llm.types import LlmMessage, LlmTool, StreamPart, UserMessage

FLASH = "qwen3.8-flash-next"
POOL = FLASH_NEXT_POOL
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


@pytest.fixture(autouse=True)
def _fresh_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fakes report token counts unrelated to the characters sent; a ratio learned from
    # them in another test would move every estimate here.
    monkeypatch.setattr(prefill, "_ratio", {})


TASKS = {
    "agent.turn": ("local", FLASH),
    "vision.ocr": ("local", FLASH),
    "research.title": ("local", FLASH),
    "debug.whatever": ("local", FLASH),
    "wiki.rewrite": ("xai", "grok-4.3"),
    "fact.adjudicate": ("local", "gpt-oss-120b"),
}


def _router(fake: Any, *, guard: KvPoolGuard | None = None, **kw: Any) -> LlmRouter:
    return LlmRouter({"local": fake, "xai": fake}, TASKS, pool_guard=guard, **kw)


def _guard(n_slots: int = POOL.n_slots) -> KvPoolGuard:
    async def read(model: str) -> list[dict[str, object]]:
        return [{"id": i, "is_processing": False, "n_prompt_tokens": 0} for i in range(n_slots)]

    async def erase(model: str, slot: int) -> bool:
        raise AssertionError("an empty pool needs no room made")

    return KvPoolGuard(read, erase)


async def test_each_task_lands_in_its_roles_slot() -> None:
    fake = FakeLlmClient()
    router = _router(fake, guard=_guard())
    await router.complete("vision.ocr", system="s", user_text="u")
    await router.complete("research.title", system="s", user_text="u")
    await router.complete("debug.whatever", system="s", user_text="u")
    assert [c["id_slot"] for c in fake.calls] == [1, 7, 5]


async def test_agent_turn_defaults_interactive_and_a_named_role_wins() -> None:
    fake = FakeLlmClient()
    router = _router(fake)
    msgs = [UserMessage("hi")]
    await router.converse("agent.turn", system="s", messages=msgs)
    await router.converse("agent.turn", system="s", messages=msgs, slot_role=SlotRole.RESEARCH)
    assert [c["id_slot"] for c in fake.converse_calls] == [0, 3]


async def test_a_prompt_over_its_slots_cap_is_refused_before_anything_is_sent() -> None:
    fake = FakeLlmClient()
    # ~162k tokens at the default 3.7 chars/token, against the ingest slot's 128k.
    with pytest.raises(SlotCapError) as caught:
        await _router(fake).complete("vision.ocr", system="x" * 600_000, user_text="u")
    assert fake.calls == []
    assert caught.value.role is SlotRole.INGEST and caught.value.cap == 131_072


async def test_output_overrunning_the_cap_is_clamped() -> None:
    fake = FakeLlmClient()
    chars = int(120_000 * 3.7)
    await _router(fake).complete("vision.ocr", system="x" * chars, user_text="", max_tokens=20_000)
    sent = fake.calls[0]["max_tokens"]
    assert 10_000 < sent < 20_000


async def test_images_count_against_the_cap() -> None:
    from jbrain.llm.types import LlmImage

    fake = FakeLlmClient()
    images = [LlmImage(media_type="image/png", data="x")] * 40  # 40 x 4096 > the 128k slot
    with pytest.raises(SlotCapError):
        await _router(fake).complete("vision.ocr", system="s", user_text="u", images=images)


async def test_a_mismatched_live_layout_sends_unpinned() -> None:
    fake = FakeLlmClient()
    await _router(fake, guard=_guard(n_slots=4)).complete(
        "research.title", system="s", user_text="u"
    )
    assert fake.calls[0]["id_slot"] is None


async def test_the_json_reask_keeps_the_slot() -> None:
    fake = FakeLlmClient(["not json", '{"ok": true}'])
    await _router(fake).complete("vision.ocr", system="s", user_text="u", json_schema=SCHEMA)
    assert [c["id_slot"] for c in fake.calls] == [1, 1]


class _TruncatingClient(FakeLlmClient):
    async def converse_stream(self, **kwargs: Any) -> AsyncIterator[StreamPart]:  # type: ignore[override]
        self.stream_calls.append(kwargs)
        raise LlmStreamTruncatedError("cut")
        yield  # pragma: no cover — makes this an async generator


async def test_the_truncated_stream_retry_keeps_the_slot() -> None:
    fake = _TruncatingClient()
    parts = [
        p
        async for p in _router(fake).converse_stream(
            "agent.turn", system="s", messages=[UserMessage("hi")], slot_role=SlotRole.SCHEDULED
        )
    ]
    assert isinstance(parts[-1], LlmTurn)
    assert fake.stream_calls[0]["id_slot"] == 2
    assert fake.converse_calls[0]["id_slot"] == 2


class _StrictClient:
    """A client with no `id_slot` parameter at all, like the test fakes written before it."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs: Any) -> Any:
        assert "id_slot" not in kwargs
        self.calls += 1
        return await FakeLlmClient().complete(**kwargs)

    async def converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = 1,
        reasoning_effort: str | None = None,
        sampling: Any = None,
    ) -> LlmTurn:
        self.calls += 1
        return LlmTurn(text="ok", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))


async def test_cloud_and_standard_engine_routes_never_carry_a_slot() -> None:
    strict = _StrictClient()
    router = _router(strict, guard=_guard())
    await router.complete("wiki.rewrite", system="s", user_text="u")
    await router.complete("fact.adjudicate", system="s", user_text="u")
    await router.converse("wiki.rewrite", system="s", messages=[UserMessage("hi")])
    assert strict.calls == 3


async def test_the_window_of_a_pooled_model_is_its_slots_cap() -> None:
    async def stale_windows() -> dict[str, int]:
        return {FLASH: 524_288}  # an override saved before the pool existed

    router = _router(FakeLlmClient(), local_windows_loader=stale_windows)
    assert await router.context_window("vision.ocr") == 131_072
    assert await router.context_window("agent.turn") == 262_144
    assert await router.context_window("agent.turn", slot_role=SlotRole.SMALL) == 65_536
    assert context_window_for_spec(f"local:{FLASH}", engines.FLASH_NEXT) == 262_144


async def test_local_usage_calibrates_the_estimate_but_an_image_call_does_not() -> None:
    from jbrain.llm.types import LlmImage

    fake = FakeLlmClient()
    router = _router(fake)
    await router.complete(
        "vision.ocr", system="s", user_text="u", images=[LlmImage("image/png", "x")]
    )
    assert FLASH not in prefill._ratio
    await router.complete("vision.ocr", system="s" * 99, user_text="u")
    assert FLASH in prefill._ratio


def _local_client(seen: list[dict[str, Any]], provider: str) -> OpenAiCompatClient:
    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return OpenAiCompatClient(
        "http://gw/v1", "", provider=provider, transport=httpx.MockTransport(handle)
    )


async def test_only_the_local_provider_puts_id_slot_on_the_wire() -> None:
    seen: list[dict[str, Any]] = []
    await _local_client(seen, "local").complete(model="m", system="s", user_text="u", id_slot=3)
    await _local_client(seen, "local").converse(
        model="m", system="s", messages=[UserMessage("u")], id_slot=4
    )
    await _local_client(seen, "local").complete(model="m", system="s", user_text="u")
    await _local_client(seen, "xai").complete(model="m", system="s", user_text="u", id_slot=3)
    assert [body.get("id_slot") for body in seen] == [3, 4, None, None]
