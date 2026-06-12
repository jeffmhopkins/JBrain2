"""The agent loop: tool dispatch, the result feedback cycle, and every guardrail
(step / cost / consecutive-error caps), driven by the fake adapter and fake
tools — no real model, no database."""

import hashlib
from typing import Any

from jbrain.agent.contracts import (
    ChatEvent,
    DoneEvent,
    NoteSource,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
    ToolSpec,
)
from jbrain.agent.loop import (
    SYSTEM_PROMPT,
    SYSTEM_VERSION,
    AgentLoop,
    Guardrails,
    ToolContext,
    ToolOutput,
)
from jbrain.agent.toolfile import ToolFile
from jbrain.agent.toolregistry import RegisteredTool, ToolHandler, ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.llm import (
    FakeLlmClient,
    LlmRouter,
    LlmTurn,
    LlmUsage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

OWNER = SessionContext(principal_kind="owner")


def make_tool(name: str, handler: ToolHandler, *, permission: str = "read") -> RegisteredTool:
    spec = ToolSpec(name=name, version=1, params={"type": "object"}, permission=permission)  # type: ignore[arg-type]
    return RegisteredTool(
        toolfile=ToolFile(spec=spec, description=f"the {name} tool"), handler=handler
    )


async def search(arguments: dict, ctx: ToolContext) -> str:
    return f"found: {arguments.get('q', '')}"


async def boom(arguments: dict, ctx: ToolContext) -> str:
    raise RuntimeError("nope")


async def search_sourced(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput("found 1", (NoteSource(note_id="n1", domain="general", snippet="hi"),))


def router_with(turns: list[LlmTurn]) -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(turns=turns)
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}), fake


def registry_with(*tools: RegisteredTool) -> ToolRegistry:
    return ToolRegistry(list(tools))


async def run(loop: AgentLoop, scopes: tuple[str, ...] = ("general",)) -> Any:
    return await loop.run(
        session=OWNER, scopes=scopes, conversation=[UserMessage(text="what do I know?")]
    )


def test_system_prompt_pinned_to_its_version() -> None:
    """The system prompt carries the data/instruction boundary (a safety policy):
    editing it must be a deliberate version bump, like every .prompt file."""
    digest = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    assert (SYSTEM_VERSION, digest) == (
        "agent-system-v1",
        "798060d1b29809ec69dacadbe2beb85301b1f21fedd521e012c9a490bbb777e4",
    )


async def test_answers_immediately_without_tools() -> None:
    router, _ = router_with([LlmTurn("here you go", (), "end_turn", LlmUsage(1, 1))])
    result = await run(AgentLoop(router, registry_with(make_tool("search", search))))
    assert result.text == "here you go"
    assert result.stop_reason == "end_turn"
    assert result.steps == 1


async def test_runs_a_tool_then_answers() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "search", {"q": "x"}),), "tool_use", LlmUsage(10, 5)),
        LlmTurn("the answer", (), "end_turn", LlmUsage(8, 3)),
    ]
    router, fake = router_with(turns)
    result = await run(AgentLoop(router, registry_with(make_tool("search", search))))
    assert result.text == "the answer"
    assert result.stop_reason == "end_turn"
    assert result.steps == 2
    assert result.cost_tokens == 26
    # The loop fed the tool result back on the second turn.
    assert isinstance(fake.converse_calls[1]["messages"][-1], ToolResultMessage)
    assert fake.converse_calls[1]["messages"][-1].results[0].content == "found: x"


async def test_only_in_scope_tools_are_offered() -> None:
    health = make_tool("read_lab", search)
    object.__setattr__(health.toolfile.spec, "domains", ["health"])  # health-only
    router, fake = router_with([LlmTurn("ok", (), "end_turn", LlmUsage(1, 1))])
    await run(
        AgentLoop(router, registry_with(make_tool("search", search), health)), scopes=("general",)
    )
    offered = {t.name for t in fake.converse_calls[0]["tools"]}
    assert offered == {"search"}  # the health tool was hidden from a general session


async def test_max_steps_guardrail_stops_a_tool_loop() -> None:
    # The model always asks for a tool; the step cap must stop it.
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1))]
    router, _ = router_with(forever)
    loop = AgentLoop(
        router, registry_with(make_tool("search", search)), guardrails=Guardrails(max_steps=3)
    )
    result = await run(loop)
    assert result.stop_reason == "max_steps"
    assert result.steps == 3


async def test_consecutive_tool_errors_guardrail() -> None:
    forever = [LlmTurn("", (ToolCall("c", "boom", {}),), "tool_use", LlmUsage(1, 1))]
    router, _ = router_with(forever)
    loop = AgentLoop(
        router,
        registry_with(make_tool("boom", boom)),
        guardrails=Guardrails(max_consecutive_tool_errors=2),
    )
    result = await run(loop)
    assert result.stop_reason == "too_many_errors"


async def test_cost_budget_guardrail() -> None:
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(10, 10))]
    router, _ = router_with(forever)
    loop = AgentLoop(
        router, registry_with(make_tool("search", search)), guardrails=Guardrails(max_cost_tokens=5)
    )
    result = await run(loop)
    assert result.stop_reason == "budget"


async def test_unknown_tool_is_a_recoverable_error() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "ghost", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("recovered", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, fake = router_with(turns)
    result = await run(AgentLoop(router, registry_with(make_tool("search", search))))
    assert result.text == "recovered"
    fed_back = fake.converse_calls[1]["messages"][-1].results[0]
    assert fed_back.is_error and "unknown tool" in fed_back.content


# --- run_stream (streaming twin) --------------------------------------------


def stream_router_with(
    turns: list[LlmTurn], stream_chunks: list[list[str]] | None = None
) -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(turns=turns, stream_chunks=stream_chunks or [])
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}), fake


async def collect(loop: AgentLoop, scopes: tuple[str, ...] = ("general",)) -> list[ChatEvent]:
    return [
        event
        async for event in loop.run_stream(
            session=OWNER, scopes=scopes, conversation=[UserMessage(text="what do I know?")]
        )
    ]


async def test_run_stream_streams_text_then_done() -> None:
    router, _ = stream_router_with(
        [LlmTurn("here you go", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["here ", "you go"]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    assert events == [
        TextDelta(text="here "),
        TextDelta(text="you go"),
        DoneEvent(stop_reason="end_turn"),
    ]


async def test_run_stream_emits_tool_call_and_result_around_dispatch() -> None:
    turns = [
        LlmTurn(
            "let me check", (ToolCall("c1", "search", {"q": "x"}),), "tool_use", LlmUsage(10, 5)
        ),
        LlmTurn("the answer", (), "end_turn", LlmUsage(8, 3)),
    ]
    router, _ = stream_router_with(turns, stream_chunks=[["let me ", "check"], ["the answer"]])
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    assert events == [
        TextDelta(text="let me "),
        TextDelta(text="check"),
        ToolCallEvent(id="c1", name="search", arguments={"q": "x"}),
        ToolResultEvent(tool_call_id="c1", ok=True, summary="found: x"),
        TextDelta(text="the answer"),
        DoneEvent(stop_reason="end_turn"),
    ]


async def test_run_stream_tool_result_carries_structured_sources() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search_sourced))))
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.summary == "found 1"
    assert result.sources == [NoteSource(note_id="n1", domain="general", snippet="hi")]


async def test_run_stream_tool_error_surfaces_in_result_event() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "boom", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("recovered", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("boom", boom))))
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is False and "nope" in result.summary
    assert events[-1] == DoneEvent(stop_reason="end_turn")


async def test_run_stream_max_steps_guardrail_emits_done() -> None:
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1))]
    router, _ = stream_router_with(forever)
    loop = AgentLoop(
        router, registry_with(make_tool("search", search)), guardrails=Guardrails(max_steps=2)
    )
    events = await collect(loop)
    assert events[-1] == DoneEvent(stop_reason="max_steps")
    # Two model turns, each emitting one tool_call + tool_result before the cap.
    assert sum(isinstance(e, ToolCallEvent) for e in events) == 2


async def test_run_stream_cost_budget_emits_done() -> None:
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(10, 10))]
    router, _ = stream_router_with(forever)
    loop = AgentLoop(
        router, registry_with(make_tool("search", search)), guardrails=Guardrails(max_cost_tokens=5)
    )
    events = await collect(loop)
    assert events[-1] == DoneEvent(stop_reason="budget")


async def test_run_stream_records_model_and_tool_steps() -> None:
    steps: list[tuple[str, str, bool]] = []

    class Recorder:
        async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
            steps.append((kind, name, ok))

    turns = [
        LlmTurn("", (ToolCall("c1", "search", {"q": "x"}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("search", search)), recorder=Recorder())
    await collect(loop)
    assert steps == [
        ("model", "converse", True),
        ("tool", "search", True),
        ("model", "converse", True),
    ]


async def test_recorder_logs_model_and_tool_steps() -> None:
    steps: list[tuple[str, str, bool]] = []

    class Recorder:
        async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
            steps.append((kind, name, ok))

    turns = [
        LlmTurn("", (ToolCall("c1", "search", {"q": "x"}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("search", search)), recorder=Recorder())
    await run(loop)
    assert steps == [
        ("model", "converse", True),
        ("tool", "search", True),
        ("model", "converse", True),
    ]
