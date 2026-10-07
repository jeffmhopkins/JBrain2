"""Tool-aware converse: router routing + usage recording, and the fake's
scripted turns driving a multi-turn tool exchange (the only LLM the agent-loop
tests will call)."""

import json
from typing import Any

import httpx

from jbrain.llm import (
    AssistantMessage,
    FakeLlmClient,
    LlmMessage,
    LlmRouter,
    LlmTool,
    LlmTurn,
    LlmUsage,
    OpenAiCompatClient,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

SEARCH = LlmTool(name="search", description="find", input_schema={"type": "object"})


def fake_router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")})


async def test_converse_routes_task_to_provider_model() -> None:
    fake = FakeLlmClient(turns=[LlmTurn("hi", (), "end_turn", LlmUsage(2, 3))])
    turn = await fake_router(fake).converse(
        "agent.turn", system="s", messages=[UserMessage(text="u")]
    )
    assert turn.text == "hi"
    assert fake.converse_calls[0]["model"] == "grok-4.3"
    assert fake.converse_calls[0]["system"] == "s"


async def test_converse_records_usage() -> None:
    records: list[tuple[str, int]] = []

    class Recorder:
        async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
            records.append((task, usage.input_tokens))

    fake = FakeLlmClient(turns=[LlmTurn("x", (), "end_turn", LlmUsage(5, 1))])
    router = LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}, recorder=Recorder())
    await router.converse("agent.turn", system="s", messages=[UserMessage(text="u")])
    assert records == [("agent.turn", 5)]


async def test_fake_scripts_a_tool_using_exchange() -> None:
    # Turn 1 requests a tool; turn 2 answers — the shape the agent loop drives.
    turns = [
        LlmTurn(
            text="",
            tool_calls=(ToolCall(id="c1", name="search", arguments={"q": "x"}),),
            stop_reason="tool_use",
            usage=LlmUsage(1, 1),
        ),
        LlmTurn(text="the answer", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1)),
    ]
    router = fake_router(fake := FakeLlmClient(turns=turns))

    messages: list = [UserMessage(text="find x")]
    first = await router.converse("agent.turn", system="s", messages=messages, tools=[SEARCH])
    assert first.stop_reason == "tool_use"
    assert first.tool_calls[0].name == "search"

    # The loop feeds back the assistant turn and the tool result, then asks again.
    messages += [
        AssistantMessage(text=first.text, tool_calls=first.tool_calls),
        ToolResultMessage(results=[ToolResult(tool_call_id="c1", content="result")]),
    ]
    second = await router.converse("agent.turn", system="s", messages=messages, tools=[SEARCH])
    assert second.stop_reason == "end_turn"
    assert second.text == "the answer"

    assert len(fake.converse_calls) == 2
    assert isinstance(fake.converse_calls[1]["messages"][-1], ToolResultMessage)


async def test_converse_captures_reasoning_content() -> None:
    # The local gateway returns the harmony reasoning on a `reasoning_content` field
    # alongside the answer; the non-stream path surfaces it on the turn.
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "the answer",
                    "reasoning_content": "let me think",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 6},
    }
    client = OpenAiCompatClient(
        "http://localhost:11434/v1",
        "",
        provider="local",
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=body)),
    )
    turn = await client.converse(model="m", system="s", messages=[UserMessage(text="u")])
    assert turn.text == "the answer"
    assert turn.reasoning == "let me think"


async def test_fake_records_reasoning_effort() -> None:
    fake = FakeLlmClient(["ok"])
    await fake.complete(model="m", system="s", user_text="u", reasoning_effort="high")
    await fake.converse(
        model="m", system="s", messages=[UserMessage(text="u")], reasoning_effort="low"
    )
    assert fake.calls[0]["reasoning_effort"] == "high"
    assert fake.converse_calls[0]["reasoning_effort"] == "low"


async def test_complete_captures_reasoning_content() -> None:
    # A one-shot against a reasoning model splits its <think> trace onto
    # reasoning_content (deepseek format); complete() keeps it OUT of `text` and
    # surfaces it on the result — so a thinking one-shot's answer stays clean.
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Giraffe Height Facts",
                    "reasoning_content": "the title should name the topic…",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 9},
    }
    client = OpenAiCompatClient(
        "http://localhost:11434/v1",
        "",
        provider="local",
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=body)),
    )
    res = await client.complete(model="qwen3.5-0.8b", system="s", user_text="u")
    assert res.text == "Giraffe Height Facts"
    assert res.reasoning == "the title should name the topic…"


def _capturing_client(provider: str = "local") -> tuple[dict[str, Any], OpenAiCompatClient]:
    """A client (local by default) whose transport records the request payload it sends,
    so a test can assert exactly what reached the gateway."""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "t"}, "finish_reason": "stop"}], "usage": {}},
        )

    client = OpenAiCompatClient(
        "http://localhost:11434/v1", "", provider=provider, transport=httpx.MockTransport(handler)
    )
    return captured, client


async def test_hybrid_qwen_maps_none_to_enable_thinking_false() -> None:
    # A Qwen hybrid toggles thinking through its chat template, not reasoning_effort.
    # "none" is the real "reasoning off": enable_thinking=false, and no reasoning_effort
    # (which the Qwen template would ignore).
    captured, client = _capturing_client()
    await client.complete(model="qwen3.5-0.8b", system="s", user_text="u", reasoning_effort="none")
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in captured["payload"]


async def test_hybrid_qwen_maps_a_level_to_enable_thinking_true() -> None:
    # A 3.5/3.6-era hybrid has no granular effort: any non-"none" level just leaves thinking
    # on, and no reasoning_effort is sent (that template genuinely ignores it).
    captured, client = _capturing_client()
    await client.converse(
        model="qwen3.5-4b",
        system="s",
        messages=[UserMessage(text="u")],
        reasoning_effort="low",
    )
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert "reasoning_effort" not in captured["payload"]


async def test_qwen38_hybrid_also_sends_a_mapped_effort_level() -> None:
    # Qwen3.8's template reads a `reasoning_effort` level from the SAME chat-template kwargs
    # bag, and applies the card's `xhigh` default when given none — which is why a trivial
    # prompt was measured spending 439 output tokens / 37.9s on this box against 161 / 13.8s
    # with thinking off. So a level map must put the mapped level on the wire beside the
    # toggle. It stays a template kwarg: the top-level field is still ignored.
    captured, client = _capturing_client()
    await client.converse(
        model="qwen3.8-27b-q4",
        system="s",
        messages=[UserMessage(text="u")],
        reasoning_effort="low",
    )
    assert captured["payload"]["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
    }
    assert "reasoning_effort" not in captured["payload"]


async def test_qwen38_hybrid_maps_our_top_level_onto_the_cards_xhigh() -> None:
    # Our four settings levels don't match the card's three. "high" is the card's top level,
    # which it spells `xhigh` — sending "high" verbatim would land on no known level and fall
    # back to the default, silently undoing the fix.
    captured, client = _capturing_client()
    await client.complete(
        model="qwen3.8-27b-q4", system="s", user_text="u", reasoning_effort="high"
    )
    assert captured["payload"]["chat_template_kwargs"]["reasoning_effort"] == "xhigh"


async def test_qwen38_hybrid_sends_no_level_when_thinking_is_off() -> None:
    # "none" is the toggle, not a level: thinking off means no effort to express, and sending
    # one alongside enable_thinking=false would be contradictory.
    captured, client = _capturing_client()
    await client.complete(
        model="qwen3.8-27b-q4", system="s", user_text="u", reasoning_effort="none"
    )
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_harmony_local_reasoner_sends_a_level_verbatim() -> None:
    # gpt-oss reads the effort level directly from the harmony template — no template kwarg.
    captured, client = _capturing_client()
    await client.complete(model="gpt-oss-120b", system="s", user_text="u", reasoning_effort="high")
    assert captured["payload"]["reasoning_effort"] == "high"
    assert "chat_template_kwargs" not in captured["payload"]


async def test_gpt_oss_maps_none_to_low_because_harmony_has_no_off() -> None:
    # gpt-oss's harmony template has no true "none": llama.cpp turns "none" into
    # enable_thinking=false + an erased effort, which the template defaults to MEDIUM — so a
    # bare "none" would silently run medium. Sending "low" (its floor) makes the pick real.
    captured, client = _capturing_client()
    await client.complete(model="gpt-oss-120b", system="s", user_text="u", reasoning_effort="none")
    assert captured["payload"]["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in captured["payload"]


async def test_glm_keeps_its_genuine_none() -> None:
    # GLM (no `no_reasoning_off`) has a real off switch its own template honors, so "none"
    # rides verbatim rather than being floored to "low".
    captured, client = _capturing_client()
    await client.complete(model="glm-4.5-air", system="s", user_text="u", reasoning_effort="none")
    assert captured["payload"]["reasoning_effort"] == "none"


# --- Preserved thinking, every turn ---------------------------------------------------

FLASH = "qwen3.8-flash-next"
_CALL = ToolCall(id="c1", name="search", arguments={"q": "x"})


def _two_turns(model: str = FLASH) -> list[LlmMessage]:
    return [
        UserMessage(text="earlier"),
        # An earlier turn's answer, replayed from the transcript with its own thinking.
        AssistantMessage(text="earlier answer", reasoning="old thinking", reasoning_model=model),
        UserMessage(text="now"),
        AssistantMessage(
            text="", tool_calls=(_CALL,), reasoning="step one thinking", reasoning_model=model
        ),
        ToolResultMessage(results=[ToolResult(tool_call_id="c1", content="r1")]),
        AssistantMessage(
            text="", tool_calls=(_CALL,), reasoning="step two thinking", reasoning_model=model
        ),
        ToolResultMessage(results=[ToolResult(tool_call_id="c1", content="r2")]),
    ]


def _assistant_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in payload["messages"] if m["role"] == "assistant"]


async def test_flash_next_replays_every_steps_reasoning_earlier_turns_included() -> None:
    # A step sent with its thinking during its own turn is sent with it on every later turn,
    # so the follow-up's prompt extends the last one instead of diverging at that step.
    captured, client = _capturing_client()
    await client.converse(model=FLASH, system="s", messages=_two_turns(), replay_reasoning=True)
    entries = _assistant_entries(captured["payload"])
    assert [e["reasoning_content"] for e in entries] == [
        "old thinking",
        "step one thinking",
        "step two thinking",
    ]
    # And the template is told to render all of it, not to drop history's thinking.
    assert captured["payload"]["chat_template_kwargs"]["preserve_thinking"] is True


async def test_the_router_decides_replay_but_the_template_kwarg_always_rides() -> None:
    # A round whose replay was dropped to fit its slot still renders history the same way.
    captured, client = _capturing_client()
    await client.converse(model=FLASH, system="s", messages=_two_turns())
    assert all("reasoning_content" not in e for e in _assistant_entries(captured["payload"]))
    assert captured["payload"]["chat_template_kwargs"] == {"preserve_thinking": True}


async def test_another_models_thinking_is_never_replayed() -> None:
    # A mid-turn engine switch: the steps were thought by gpt-oss, the next round is Flash-Next.
    captured, client = _capturing_client()
    await client.converse(
        model=FLASH, system="s", messages=_two_turns("gpt-oss-120b"), replay_reasoning=True
    )
    assert all("reasoning_content" not in e for e in _assistant_entries(captured["payload"]))


async def test_preserve_thinking_rides_beside_the_reasoning_toggle() -> None:
    captured, client = _capturing_client()
    await client.converse(
        model=FLASH,
        system="s",
        messages=_two_turns(),
        reasoning_effort="low",
        replay_reasoning=True,
    )
    assert captured["payload"]["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
        "preserve_thinking": True,
    }


async def test_flash_next_stream_replays_reasoning_too() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(req.content)
        body = (
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = OpenAiCompatClient(
        "http://localhost:11434/v1", "", provider="local", transport=httpx.MockTransport(handler)
    )
    async for _part in client.converse_stream(
        model=FLASH, system="s", messages=_two_turns(), replay_reasoning=True
    ):
        pass
    entries = _assistant_entries(captured["payload"])
    assert [e.get("reasoning_content") for e in entries] == [
        "old thinking",
        "step one thinking",
        "step two thinking",
    ]


async def test_a_step_with_no_trace_sends_no_reasoning_field() -> None:
    captured, client = _capturing_client()
    messages = [UserMessage(text="u"), AssistantMessage(text="", tool_calls=(_CALL,))]
    await client.converse(model=FLASH, system="s", messages=messages, replay_reasoning=True)
    assert "reasoning_content" not in _assistant_entries(captured["payload"])[0]


async def test_a_non_preserving_local_model_gets_no_reasoning_back() -> None:
    # Qwen3.8-27B shares the family but its served template is not shown to preserve
    # reasoning, so it keeps the pre-existing wire shape exactly — even if asked to replay.
    for model in ("qwen3.8-27b-q4", "gpt-oss-120b", "not-in-the-catalog"):
        captured, client = _capturing_client()
        await client.converse(
            model=model, system="s", messages=_two_turns(model), replay_reasoning=True
        )
        assert all("reasoning_content" not in e for e in _assistant_entries(captured["payload"]))
        assert "preserve_thinking" not in captured["payload"].get("chat_template_kwargs", {})


async def test_a_cloud_provider_never_gets_reasoning_back() -> None:
    # Even under the Flash-Next served name, a cloud provider is not the local template.
    captured, client = _capturing_client(provider="xai")
    await client.converse(model=FLASH, system="s", messages=_two_turns(), replay_reasoning=True)
    assert all("reasoning_content" not in e for e in _assistant_entries(captured["payload"]))
    assert "chat_template_kwargs" not in captured["payload"]
