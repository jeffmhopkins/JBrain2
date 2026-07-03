"""Tool-aware converse: router routing + usage recording, and the fake's
scripted turns driving a multi-turn tool exchange (the only LLM the agent-loop
tests will call)."""

import json
from typing import Any

import httpx

from jbrain.llm import (
    AssistantMessage,
    FakeLlmClient,
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


def _capturing_client() -> tuple[dict[str, Any], OpenAiCompatClient]:
    """A local client whose transport records the request payload it sends, so a
    test can assert exactly what reached the gateway."""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "t"}, "finish_reason": "stop"}], "usage": {}},
        )

    client = OpenAiCompatClient(
        "http://localhost:11434/v1", "", provider="local", transport=httpx.MockTransport(handler)
    )
    return captured, client


async def test_hybrid_qwen_maps_none_to_enable_thinking_false() -> None:
    # A Qwen hybrid toggles thinking through its chat template, not reasoning_effort.
    # "none" is the real "reasoning off": enable_thinking=false, and no reasoning_effort
    # (which the Qwen template would ignore).
    captured, client = _capturing_client()
    await client.complete(
        model="qwen3.5-0.8b", system="s", user_text="u", reasoning_effort="none"
    )
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in captured["payload"]


async def test_hybrid_qwen_maps_a_level_to_enable_thinking_true() -> None:
    # Any non-"none" level leaves thinking on (a hybrid has no granular effort).
    captured, client = _capturing_client()
    await client.converse(
        model="qwen3.5-4b",
        system="s",
        messages=[UserMessage(text="u")],
        reasoning_effort="low",
    )
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert "reasoning_effort" not in captured["payload"]


async def test_harmony_local_reasoner_sends_effort_verbatim() -> None:
    # gpt-oss understands the effort levels (incl. "none") directly — no template kwarg.
    captured, client = _capturing_client()
    await client.complete(
        model="gpt-oss-120b", system="s", user_text="u", reasoning_effort="none"
    )
    assert captured["payload"]["reasoning_effort"] == "none"
    assert "chat_template_kwargs" not in captured["payload"]
