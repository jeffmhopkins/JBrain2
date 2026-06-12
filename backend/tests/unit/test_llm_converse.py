"""Tool-aware converse: router routing + usage recording, and the fake's
scripted turns driving a multi-turn tool exchange (the only LLM the agent-loop
tests will call)."""

from jbrain.llm import (
    AssistantMessage,
    FakeLlmClient,
    LlmRouter,
    LlmTool,
    LlmTurn,
    LlmUsage,
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
