"""The agent loop: tool dispatch, the result feedback cycle, and every guardrail
(step / cost / consecutive-error caps), driven by the fake adapter and fake
tools — no real model, no database."""

import asyncio
import contextlib
import hashlib
from typing import Any

from jbrain.agent.contracts import (
    ChatEvent,
    DoneEvent,
    EntityRef,
    GeneralKnowledgeEvent,
    JobEnqueuedEvent,
    NoteSource,
    ProposalRef,
    ReasoningDelta,
    SubagentSpawnedEvent,
    TextDelta,
    ToolCallEvent,
    ToolProgressEvent,
    ToolResultEvent,
    ToolSpec,
    ToolViewEvent,
    UsageEvent,
    VerdictEvent,
    ViewPayload,
    WebSource,
)
from jbrain.agent.loop import (
    SYSTEM_PROMPT,
    SYSTEM_VERSION,
    AgentLoop,
    Guardrails,
    JobRef,
    ToolContext,
    ToolOutput,
    guardrails_for_effort,
)
from jbrain.agent.toolfile import ToolFile
from jbrain.agent.toolregistry import RegisteredTool, ToolHandler, ToolRegistry
from jbrain.agent.tree import TreeState
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


def make_tool(
    name: str, handler: ToolHandler, *, permission: str = "read", mutating: bool = False
) -> RegisteredTool:
    spec = ToolSpec(
        name=name,
        version=1,
        params={"type": "object"},
        permission=permission,  # type: ignore[arg-type]
        mutating=mutating,
    )
    return RegisteredTool(
        toolfile=ToolFile(spec=spec, description=f"the {name} tool"), handler=handler
    )


async def search(arguments: dict, ctx: ToolContext) -> str:
    return f"found: {arguments.get('q', '')}"


async def boom(arguments: dict, ctx: ToolContext) -> str:
    raise RuntimeError("nope")


async def search_sourced(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput("found 1", (NoteSource(note_id="n1", domain="general", snippet="hi"),))


async def web_sourced(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput(
        "web results",
        web_sources=(WebSource(url="https://x.example/a", title="A page"),),
    )


async def propose_sourced(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput("staged it", proposal=ProposalRef(proposal_id="p9", kind="correction"))


async def view_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput(
        "the list",
        view=ViewPayload(view="list_card", data={"title": "Groceries"}),
    )


async def job_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput("queued the export", job=JobRef(job_id="j7", summary="exporting your notes"))


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
        "agent-system-v6",
        "5d3056298e2fb0afb311f85970acecc5d2ee92bf50118d532b1268e054e875cc",
    )


def test_system_prompt_states_current_truth_arbitration() -> None:
    """v3: the agent must treat the entity graph (kept current by supersession +
    review) as the arbiter of truth, not raw note prose — the disposition behind
    the retrieval tools' currency overlay."""
    assert "arbiter of what is true today" in SYSTEM_PROMPT
    assert "read_entity to confirm" in SYSTEM_PROMPT
    assert "a superseded or retracted claim as if it were current" in SYSTEM_PROMPT


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


async def test_on_usage_reports_each_model_calls_fill() -> None:
    # Two model calls (a tool turn, then the answer): on_usage fires per call with that
    # call's prompt + output — the per-step context fill the sub-agent fan's meter reads.
    turns = [
        LlmTurn("", (ToolCall("c1", "search", {"q": "x"}),), "tool_use", LlmUsage(10, 5)),
        LlmTurn("the answer", (), "end_turn", LlmUsage(8, 3)),
    ]
    router, _ = router_with(turns)
    seen: list[tuple[int, int]] = []
    await AgentLoop(router, registry_with(make_tool("search", search))).run(
        session=OWNER,
        scopes=("general",),
        conversation=[UserMessage(text="hi")],
        on_usage=lambda inp, out: seen.append((inp, out)),
    )
    assert seen == [(10, 5), (8, 3)]


async def test_only_in_scope_tools_are_offered() -> None:
    health = make_tool("read_lab", search)
    object.__setattr__(health.toolfile.spec, "domains", ["health"])  # health-only
    router, fake = router_with([LlmTurn("ok", (), "end_turn", LlmUsage(1, 1))])
    await run(
        AgentLoop(router, registry_with(make_tool("search", search), health)), scopes=("general",)
    )
    offered = {t.name for t in fake.converse_calls[0]["tools"]}
    assert offered == {"search"}  # the health tool was hidden from a general session


def test_guardrails_for_effort_widens_the_step_cap() -> None:
    # A model set to think harder earns a deeper tool budget; low/none/non-reasoning
    # keep the default 20.
    assert guardrails_for_effort("high").max_steps == 40
    assert guardrails_for_effort("medium").max_steps == 30
    assert guardrails_for_effort("low").max_steps == 20
    assert guardrails_for_effort("none").max_steps == 20
    assert guardrails_for_effort(None).max_steps == 20
    # Default scale=1 leaves the cost/error caps at the shared defaults.
    assert guardrails_for_effort("high").max_cost_tokens == Guardrails().max_cost_tokens
    assert guardrails_for_effort("high").max_consecutive_tool_errors == 3


def test_guardrails_for_effort_scales_both_caps_per_agent() -> None:
    # A persona's budget_multiplier widens BOTH the step cap and the cost budget
    # together (the archivist runs at 4), so a long mailbox cleanup isn't cut off
    # mid-chain. The error cap stays fixed — a wedged chain still bails fast.
    g = guardrails_for_effort("high", scale=4)
    assert g.max_steps == 160
    assert g.max_cost_tokens == Guardrails().max_cost_tokens * 4
    assert g.max_consecutive_tool_errors == 3
    # The scale applies to the default step cap too, not only the effort tiers.
    assert guardrails_for_effort(None, scale=4).max_steps == 80
    assert guardrails_for_effort("medium", scale=4).max_steps == 120


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


async def test_run_streams_text_and_reasoning_to_callbacks() -> None:
    # With on_text/on_reasoning, run() drives the streaming model path and forwards each
    # chunk live (the sub-agent fan uses this to show a child working), while still
    # returning the same settled answer.
    turns = [LlmTurn("the answer", (), "end_turn", LlmUsage(1, 1))]
    router, _ = router_with(turns)
    loop = AgentLoop(router, registry_with())
    text: list[str] = []
    result = await loop.run(
        session=OWNER,
        scopes=("general",),
        conversation=[UserMessage(text="q")],
        on_text=text.append,
    )
    assert "".join(text) == "the answer"
    assert result.text == "the answer"


async def test_force_final_answer_synthesizes_on_step_exhaustion() -> None:
    # A child that keeps tool-calling hits max_steps; with force_final_answer the loop
    # makes one final no-tools turn so the caller gets a real answer, not an empty one.
    turns = [
        LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("here is what I found", (), "end_turn", LlmUsage(1, 1)),  # the forced final
    ]
    router, fake = router_with(turns)
    loop = AgentLoop(
        router, registry_with(make_tool("search", search)), guardrails=Guardrails(max_steps=2)
    )
    result = await loop.run(
        session=OWNER,
        scopes=("general",),
        conversation=[UserMessage(text="q")],
        force_final_answer=True,
    )
    assert result.stop_reason == "max_steps"
    assert result.text == "here is what I found"
    # The forced final turn was made with NO tools offered, and at NONE effort (the
    # synthesis is mechanical — any thinking there caused a long apparent stall on box).
    assert fake.converse_calls[-1]["tools"] == []
    assert fake.converse_calls[-1]["reasoning_effort"] == "none"
    # …and it carries an explicit "synthesize as prose, no tool calls" directive as the
    # last user turn, so gpt-oss writes an answer instead of emitting its next search as
    # text (which surfaced as a raw {"query": …} child answer on the box).
    last_msg = fake.converse_calls[-1]["messages"][-1]
    assert isinstance(last_msg, UserMessage) and "no more tools" in last_msg.text.lower()


async def test_soft_landing_nudges_a_child_to_finish_before_the_cap() -> None:
    # A few steps before the hard cap, a force-final-eligible run (a sub-agent) is asked
    # to wrap up — so it can land on end_turn instead of being force-cut at max_steps.
    # With max_steps=5 the warning fires at step 5-3=2 (3rd converse); the model then
    # stops calling tools and answers, so the run ends clean rather than truncated.
    turns = [
        LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("here is my answer", (), "end_turn", LlmUsage(1, 1)),  # complies with the nudge
    ]
    router, fake = router_with(turns)
    loop = AgentLoop(
        router, registry_with(make_tool("search", search)), guardrails=Guardrails(max_steps=5)
    )
    result = await loop.run(
        session=OWNER,
        scopes=("general",),
        conversation=[UserMessage(text="q")],
        force_final_answer=True,
    )
    # It ended cleanly on its own (end_turn), NOT force-cut at the cap (max_steps).
    assert result.stop_reason == "end_turn"
    assert result.text == "here is my answer"
    # The 3rd converse (index 2, where the nudge fires) carried the budget warning as its
    # last user turn; the earlier converses did not.
    assert isinstance(fake.converse_calls[2]["messages"][-1], UserMessage)
    assert "out of tool-call budget" in fake.converse_calls[2]["messages"][-1].text.lower()
    assert all(
        not (isinstance(m, UserMessage) and "budget" in m.text.lower())
        for m in fake.converse_calls[0]["messages"]
    )


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
    assert fed_back.is_error and "tool not available" in fed_back.content


async def test_a_tool_outside_the_allowlist_is_refused_at_dispatch() -> None:
    """The allowlist is a dispatch-time boundary, not just visibility: a model that
    names a registered tool it was NOT granted is refused, never run — so a
    knowledge agent can't reach a web tool it wasn't allowlisted (the #9 guard)."""
    calls: list[str] = []

    async def web_handler(arguments: dict, ctx: ToolContext) -> str:
        calls.append("ran")
        return "should never run"

    turns = [
        LlmTurn("", (ToolCall("c1", "web_search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, fake = router_with(turns)
    registry = registry_with(
        make_tool("search", search), make_tool("web_search", web_handler, permission="web")
    )
    # allow=None (the default knowledge agent) never admits the web tool.
    result = await AgentLoop(router, registry).run(
        session=OWNER, scopes=("general",), conversation=[UserMessage(text="hi")]
    )
    assert result.text == "done"
    assert calls == []  # the handler never ran
    fed_back = fake.converse_calls[1]["messages"][-1].results[0]
    assert fed_back.is_error and "tool not available" in fed_back.content


async def test_buffer_retry_path_honors_persona_prompt_and_allowlist() -> None:
    """The buffered (reflexion) produce-step must use the selected agent's prompt
    and tool allowlist, exactly like the live-stream path."""
    router, fake = router_with([LlmTurn("a plain answer", (), "end_turn", LlmUsage(1, 1))])
    registry = registry_with(
        make_tool("search", search), make_tool("web_search", search, permission="web")
    )
    events = [
        ev
        async for ev in AgentLoop(router, registry).run_stream(
            session=OWNER,
            scopes=("general",),
            conversation=[UserMessage(text="hi")],
            buffer_retry=True,
            system="PERSONA PROMPT",
            tools_allow=frozenset({"web_search"}),
        )
    ]
    assert any(e.type == "done" for e in events)
    # The buffered path goes through converse (not converse_stream).
    assert fake.converse_calls[0]["system"] == "PERSONA PROMPT"
    assert {t.name for t in fake.converse_calls[0]["tools"]} == {"web_search"}


# --- run_stream (streaming twin) --------------------------------------------


def stream_router_with(
    turns: list[LlmTurn], stream_chunks: list[list[str]] | None = None
) -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(turns=turns, stream_chunks=stream_chunks or [])
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}), fake


async def collect(
    loop: AgentLoop, scopes: tuple[str, ...] = ("general",), message: str = "what do I know?"
) -> list[ChatEvent]:
    return [
        event
        async for event in loop.run_stream(
            session=OWNER, scopes=scopes, conversation=[UserMessage(text=message)]
        )
    ]


async def _progress_tool(arguments: dict, ctx: ToolContext) -> str:
    # A tool that reports progress mid-execution: an image-gen step+preview tick, then
    # a multi-phase tool's text label tick (analyze_video's pattern).
    assert ctx.emit_progress is not None
    ctx.emit_progress(5, 20, "data:image/jpeg;base64,AAA", None)
    ctx.emit_progress(0, 0, None, "Analyzing frame 12/30")
    return "rendered"


async def test_run_stream_interleaves_tool_progress_before_the_result() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "render", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("render", _progress_tool)))
    events = await collect(loop)

    progress = [e for e in events if isinstance(e, ToolProgressEvent)]
    assert [(p.step, p.total, p.preview, p.label) for p in progress] == [
        (5, 20, "data:image/jpeg;base64,AAA", None),
        (0, 0, None, "Analyzing frame 12/30"),
    ]
    assert all(p.tool_call_id == "c1" for p in progress)
    # Every progress tick lands BEFORE the tool's result (it streamed while running).
    last_progress = max(i for i, e in enumerate(events) if isinstance(e, ToolProgressEvent))
    first_result = next(i for i, e in enumerate(events) if isinstance(e, ToolResultEvent))
    assert last_progress < first_result


async def test_run_stream_cancels_an_in_flight_tool_when_the_turn_is_cancelled() -> None:
    # The tool dispatch runs as its own task while the loop drains its live events. A
    # turn cancelled mid-tool (an explicit Stop) must propagate INTO that task — else a
    # spawn_subagent fan's children keep grinding the GPU long after the parent ended.
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocks(arguments: dict, ctx: ToolContext) -> str:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    turns = [LlmTurn("", (ToolCall("c1", "blocks", {}),), "tool_use", LlmUsage(1, 1))]
    router, _ = stream_router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("blocks", blocks)))

    async def drive() -> None:
        async for _event in loop.run_stream(
            session=OWNER, scopes=("general",), conversation=[UserMessage(text="go")]
        ):
            pass

    task = asyncio.ensure_future(drive())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # The in-flight tool saw the cancellation (its own task was cancelled, not just the
    # loop's await) — so a sub-agent fan dispatched here would tear down its children.
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert cancelled.is_set()


async def test_run_stream_emits_no_progress_for_a_silent_tool() -> None:
    # A tool that never calls emit_progress yields no ToolProgressEvent — the
    # interleave is invisible to every existing tool.
    turns = [
        LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    events = await collect(loop)
    assert not any(isinstance(e, ToolProgressEvent) for e in events)


async def test_run_stream_emits_usage_when_a_context_window_is_given() -> None:
    # With a context window, each model turn rides a UsageEvent the PWA's meter
    # reads — here a two-step turn emits one per step, carrying that step's prompt
    # (the context-fill numerator) and the window denominator.
    turns = [
        LlmTurn("checking", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1000, 50)),
        LlmTurn("done", (), "end_turn", LlmUsage(1800, 20)),
    ]
    router, _ = stream_router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    events = [
        event
        async for event in loop.run_stream(
            session=OWNER,
            scopes=("general",),
            conversation=[UserMessage(text="what do I know?")],
            context_window=32768,
        )
    ]
    usage = [e for e in events if isinstance(e, UsageEvent)]
    assert usage == [
        UsageEvent(input_tokens=1000, output_tokens=50, context_window=32768),
        UsageEvent(input_tokens=1800, output_tokens=20, context_window=32768),
    ]


async def test_run_stream_omits_usage_without_a_context_window() -> None:
    # The default (no window): the stream is byte-for-byte what it was before — no
    # UsageEvent — so existing callers/tests are untouched.
    loop = AgentLoop(router_with([])[0], registry_with(make_tool("search", search)))
    events = await collect(loop)
    assert not any(isinstance(e, UsageEvent) for e in events)


async def test_run_stream_streams_text_then_done() -> None:
    router, _ = stream_router_with(
        [LlmTurn("here you go", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["here ", "you go"]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    # A zero-retrieval substantive answer now carries the neutral general-knowledge
    # label after `done` (its own dedicated test covers the gating; here it just
    # rides at the tail of the otherwise-mechanical stream).
    assert events == [
        TextDelta(text="here "),
        TextDelta(text="you go"),
        DoneEvent(stop_reason="end_turn"),
        GeneralKnowledgeEvent(),
    ]


async def test_run_stream_emits_reasoning_before_answer() -> None:
    # A reasoning-capable turn streams its thinking trace; the loop relays it as a
    # ReasoningDelta ahead of the answer text, never folding it into the answer.
    router, _ = stream_router_with(
        [LlmTurn("the answer", (), "end_turn", LlmUsage(1, 1), reasoning="let me think")],
        stream_chunks=[["the answer"]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    assert events[0] == ReasoningDelta(text="let me think")
    assert events[1] == TextDelta(text="the answer")
    # The reasoning text never leaks into the answer deltas.
    assert all(e.text != "let me think" for e in events if isinstance(e, TextDelta))


async def test_run_stream_emits_tool_call_and_result_around_dispatch() -> None:
    turns = [
        LlmTurn(
            "let me check", (ToolCall("c1", "search", {"q": "x"}),), "tool_use", LlmUsage(10, 5)
        ),
        LlmTurn("the answer", (), "end_turn", LlmUsage(8, 3)),
    ]
    router, _ = stream_router_with(turns, stream_chunks=[["let me ", "check"], ["the answer"]])
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    # `search` here surfaces no sources (a plain str), so the grounding corpus is
    # empty; the substantive answer therefore tails with the neutral general-
    # knowledge label after `done`.
    assert events == [
        TextDelta(text="let me "),
        TextDelta(text="check"),
        ToolCallEvent(id="c1", name="search", arguments={"q": "x"}),
        ToolResultEvent(tool_call_id="c1", ok=True, summary="found: x"),
        TextDelta(text="the answer"),
        DoneEvent(stop_reason="end_turn"),
        GeneralKnowledgeEvent(),
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


async def test_run_stream_tool_result_carries_web_sources() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "web_search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("web_search", web_sourced))))
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.web_sources == [WebSource(url="https://x.example/a", title="A page")]


async def test_run_stream_tool_result_carries_a_staged_proposal() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "propose", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("propose", propose_sourced))))
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.proposal == ProposalRef(proposal_id="p9", kind="correction")


async def test_run_stream_emits_a_tool_view_after_its_result() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "read_list", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("read_list", view_tool))))
    # The view rides as its own event, right after the result it belongs to.
    types = [type(e).__name__ for e in events]
    assert types.index("ToolViewEvent") == types.index("ToolResultEvent") + 1
    view = next(e for e in events if isinstance(e, ToolViewEvent))
    assert view.tool_call_id == "c1" and view.view.view == "list_card"
    assert view.view.data == {"title": "Groceries"}


async def test_run_stream_emits_job_enqueued_when_a_tool_defers() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "export", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("on it", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("export", job_tool))))
    # The deferred-job event rides right after the result of the tool that enqueued it.
    types = [type(e).__name__ for e in events]
    assert types.index("JobEnqueuedEvent") == types.index("ToolResultEvent") + 1
    job = next(e for e in events if isinstance(e, JobEnqueuedEvent))
    assert job.job_id == "j7" and job.summary == "exporting your notes"


async def test_run_stream_no_job_event_when_tool_enqueues_nothing() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search_sourced))))
    assert not any(isinstance(e, JobEnqueuedEvent) for e in events)


async def test_run_stream_no_view_when_tool_has_none() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search_sourced))))
    assert not any(isinstance(e, ToolViewEvent) for e in events)


async def test_run_stream_tool_error_surfaces_in_result_event() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "boom", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("recovered", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    events = await collect(AgentLoop(router, registry_with(make_tool("boom", boom))))
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is False and "nope" in result.summary
    # The stream still settles cleanly after a recovered tool error. (The
    # substantive "recovered" answer retrieved nothing, so a neutral
    # general-knowledge label tails after the terminal done.)
    assert events[-2] == DoneEvent(stop_reason="end_turn")
    assert isinstance(events[-1], GeneralKnowledgeEvent)


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


# --- Reflexion (Loop 1) verify-and-annotate, default mode (b) ----------------


async def search_cholesterol(arguments: dict, ctx: ToolContext) -> ToolOutput:
    """A read that surfaces a source whose snippet the answer can (mis)ground in."""
    return ToolOutput(
        "found it",
        (NoteSource(note_id="n1", domain="health", snippet="cholesterol reading is elevated"),),
    )


async def stage_correction(arguments: dict, ctx: ToolContext) -> ToolOutput:
    return ToolOutput("staged it", proposal=ProposalRef(proposal_id="p1", kind="correction"))


async def stage_correction_sourced(arguments: dict, ctx: ToolContext) -> ToolOutput:
    """A mutating tool that also surfaces a source — so the turn is critique-worthy
    via the mutation AND has a non-empty corpus for the grounding check to run."""
    return ToolOutput(
        "staged it",
        (NoteSource(note_id="n1", domain="general", snippet="cholesterol reading is elevated"),),
        proposal=ProposalRef(proposal_id="p1", kind="correction"),
    )


async def find_me(arguments: dict, ctx: ToolContext) -> ToolOutput:
    """A graph answer: find_entity surfaces the owner entity with its aliases (the
    real-world name forms) and ZERO note sources — the bug case."""
    return ToolOutput(
        "- Me [person] (general)",
        entities=(
            EntityRef(
                entity_id="e1",
                label="Me",
                domain="general",
                aliases=["Jeffrey Mark Hopkins", "Jeff"],
            ),
        ),
    )


def _entity_turns() -> list[LlmTurn]:
    return [
        LlmTurn("", (ToolCall("c1", "find_entity", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("answer", (), "end_turn", LlmUsage(1, 1)),
    ]


def _sourced_turns() -> list[LlmTurn]:
    return [
        LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("answer", (), "end_turn", LlmUsage(1, 1)),
    ]


async def test_non_critique_turn_emits_no_verdict_and_streams_identically() -> None:
    # A greeting: no sources, no mutation, general scope only — the stream is
    # exactly what it was before reflexion (no tail verdict).
    router, _ = stream_router_with(
        [LlmTurn("hello there", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["hello ", "there"]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    assert events == [
        TextDelta(text="hello "),
        TextDelta(text="there"),
        DoneEvent(stop_reason="end_turn"),
    ]
    assert not any(isinstance(e, VerdictEvent) for e in events)


async def test_ungrounded_critique_turn_emits_a_verdict_after_done() -> None:
    # The turn surfaced a source (critique-worthy) but the answer's claim grounds
    # in nothing the source said → a tail VerdictEvent after DoneEvent.
    router, _ = stream_router_with(
        _sourced_turns(),
        stream_chunks=[[""], ["the roof needs replacing soon"]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("search", search_cholesterol)))
    )
    assert isinstance(events[-2], DoneEvent)
    verdict = events[-1]
    assert isinstance(verdict, VerdictEvent)
    assert verdict.passed is False
    assert any("not grounded" in i for i in verdict.issues)
    # The structured field carries the verbatim ungrounded answer sentence (the PWA
    # anchors its inline flag against this), not the prose-prefixed issue string.
    assert verdict.ungrounded_claims == ["the roof needs replacing soon"]


async def test_grounded_critique_turn_emits_no_verdict() -> None:
    # Same critique-worthy turn, but the answer grounds in the source → a clean
    # pass, so no verdict is emitted (nothing to annotate).
    router, _ = stream_router_with(
        _sourced_turns(),
        stream_chunks=[[""], ["your cholesterol reading is elevated"]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("search", search_cholesterol)))
    )
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, VerdictEvent) for e in events)


async def test_touching_a_sensitive_source_makes_the_turn_critique_worthy() -> None:
    # The turn surfaced a health-domain source → touched_sensitive → critique-worthy,
    # so an ungrounded answer (it ignored the cholesterol snippet) surfaces a verdict.
    router, _ = stream_router_with(
        _sourced_turns(),
        stream_chunks=[[""], ["the roof needs replacing soon"]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("search", search_cholesterol))),
        scopes=("health",),
    )
    assert isinstance(events[-1], VerdictEvent) and events[-1].passed is False


async def test_held_sensitive_scope_without_retrieval_labels_general_knowledge() -> None:
    # A broadly-scoped (Full Brain) session that retrieved nothing: no sources, no
    # entities, no mutation. The touched-sensitive trigger does not fire (nothing
    # sensitive was actually touched) and the empty-corpus guard keeps grounding
    # unverifiable — so NO amber verdict (#226: don't cry wolf). But the answer is a
    # substantive claim from the model's own knowledge, so it now carries the neutral
    # general-knowledge provenance label instead of passing silently.
    router, _ = stream_router_with(
        [LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["the roof needs replacing"]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("search", search))),
        scopes=("general", "health", "finance", "location"),
    )
    assert not any(isinstance(e, VerdictEvent) for e in events)
    assert isinstance(events[-2], DoneEvent)
    assert isinstance(events[-1], GeneralKnowledgeEvent)


async def test_zero_retrieval_substantive_turn_labels_general_knowledge() -> None:
    # "What does Jeff stand for?" answered from the model's own world knowledge — no
    # tools, no sources, no entities. The answer makes a substantive claim, so a
    # single neutral GeneralKnowledgeEvent rides after done, and NO amber verdict.
    router, _ = stream_router_with(
        [LlmTurn("Jeff is a short form of Jeffrey.", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["Jeff is a short form of Jeffrey."]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    assert isinstance(events[-2], DoneEvent)
    assert isinstance(events[-1], GeneralKnowledgeEvent)
    assert not any(isinstance(e, VerdictEvent) for e in events)
    # Exactly one label — the two signals never co-occur on a turn.
    assert sum(isinstance(e, GeneralKnowledgeEvent) for e in events) == 1


async def test_general_knowledge_label_suppressed_for_a_non_kb_agent() -> None:
    # The same zero-retrieval substantive turn, but with general_knowledge_label=False
    # (a non-KB agent like jerv/teacher): no "not your notes" chip — there are no notes
    # to contrast with — yet the answer still streams and `done` still closes the turn.
    router, _ = stream_router_with(
        [LlmTurn("Jeff is a short form of Jeffrey.", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["Jeff is a short form of Jeffrey."]],
    )
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    events = [
        ev
        async for ev in loop.run_stream(
            session=OWNER,
            scopes=("general",),
            conversation=[UserMessage(text="what is jeff?")],
            general_knowledge_label=False,
        )
    ]
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, GeneralKnowledgeEvent) for e in events)


async def test_greeting_emits_no_general_knowledge_label() -> None:
    # A pure greeting (no substantive claim) retrieved nothing, but it carries no
    # checkable knowledge — so it stays silent: no label, no verdict.
    router, _ = stream_router_with(
        [LlmTurn("hello there!", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["hello there!"]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("search", search))))
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, GeneralKnowledgeEvent) for e in events)
    assert not any(isinstance(e, VerdictEvent) for e in events)


async def test_graph_answered_turn_labels_no_general_knowledge() -> None:
    # The "What is my name?" turn grounded in retrieved entity aliases (non-empty
    # corpus) — neither the neutral label (that's zero-retrieval only) nor the amber
    # verdict (it grounded). The bubble shows nothing.
    router, _ = stream_router_with(
        _entity_turns(),
        stream_chunks=[[""], ["Your name is Jeffrey Mark Hopkins (Jeff)."]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("find_entity", find_me))))
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, (GeneralKnowledgeEvent, VerdictEvent)) for e in events)


async def test_retrieved_ungrounded_turn_shows_verdict_not_general_knowledge() -> None:
    # A genuinely ungrounded RETRIEVED claim still flags amber (verdict), never the
    # neutral label — the two are mutually exclusive and retrieval picks the verdict.
    router, _ = stream_router_with(
        _sourced_turns(),
        stream_chunks=[[""], ["the roof needs replacing soon"]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("search", search_cholesterol)))
    )
    assert isinstance(events[-1], VerdictEvent) and events[-1].passed is False
    assert not any(isinstance(e, GeneralKnowledgeEvent) for e in events)


async def test_a_staged_mutation_makes_the_turn_critique_worthy() -> None:
    # A staged proposal carries a write → critique-worthy even in general scope. The
    # tool also surfaces a source (a non-empty corpus), so the ungrounded answer is
    # actually checkable and flags.
    router, _ = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "correct", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("unrelated prose", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["the unrelated prose stands alone"]],
    )
    events = await collect(
        AgentLoop(
            router,
            registry_with(
                make_tool("correct", stage_correction_sourced, permission="mutate", mutating=True)
            ),
        )
    )
    assert any(isinstance(e, VerdictEvent) for e in events)


async def test_graph_answered_turn_grounds_against_entity_aliases() -> None:
    # The headline fix: "What is my name?" answered "Jeffrey Mark Hopkins (Jeff)"
    # straight from the entity graph — entities surfaced (critique-worthy via entity
    # evidence), zero note sources. The answer grounds against the entity's aliases,
    # so NO VerdictEvent is emitted (it was being falsely flagged "not in your
    # notes" when grounding ran against an empty note-only corpus).
    router, _ = stream_router_with(
        _entity_turns(),
        stream_chunks=[[""], ["Your name is Jeffrey Mark Hopkins (Jeff)."]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("find_entity", find_me))))
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, VerdictEvent) for e in events)


async def read_birthdate(arguments: dict, ctx: ToolContext) -> ToolOutput:
    """read_entity surfacing the owner subject with a current fact whose VALUE the
    answer quotes (a birth date) — the screenshot bug: the fact text must be in the
    grounding corpus or "born in 1986" falsely flags "not in your notes"."""
    return ToolOutput(
        "Me [Person] (general)\nfacts:\n- birthDate: Jeff's birth date is 1986-03-19",
        entities=(
            EntityRef(
                entity_id="e1",
                label="Me",
                domain="general",
                aliases=["Jeff"],
                facts=["Jeff's birth date is 1986-03-19"],
            ),
        ),
    )


async def test_graph_answer_citing_a_surfaced_entity_grounds() -> None:
    # The screenshot case verbatim: "What year was I born?" answered from the read
    # entity's birthDate fact, with a 【^1】 marker citing that surfaced entity. The
    # rephrased date ("March 19, 1986" vs the fact's ISO "1986-03-19") can't reach the
    # token-overlap bar, but the citation resolves to source #1 — so it grounds by
    # attribution and NO VerdictEvent / GeneralKnowledgeEvent is emitted (it was being
    # falsely flagged "not in your notes").
    router, _ = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "read_entity", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("answer", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["You were born in 1986 — specifically on March 19, 1986 【^1】."]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("read_entity", read_birthdate)))
    )
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, VerdictEvent) for e in events)
    assert not any(isinstance(e, GeneralKnowledgeEvent) for e in events)


async def test_graph_answer_reusing_fact_tokens_grounds_without_a_citation() -> None:
    # Even uncited, a fact-value answer grounds when it reuses the fact's tokens: the
    # read entity's fact statement is now in the corpus, so "Your birth date is
    # 1986-03-19" overlaps it. (Before the fix the corpus held only name + aliases.)
    router, _ = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "read_entity", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("answer", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["Your birth date is 1986-03-19."]],
    )
    events = await collect(
        AgentLoop(router, registry_with(make_tool("read_entity", read_birthdate)))
    )
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, VerdictEvent) for e in events)


async def test_graph_answer_naming_an_unretrieved_entity_still_flags() -> None:
    # The entity corpus does not paper over genuine hallucination: a name NOT among
    # any retrieved entity/note (here the answer claims a different person) fails
    # grounding and still surfaces a verdict.
    router, _ = stream_router_with(
        _entity_turns(),
        stream_chunks=[[""], ["Your name is Napoleon Bonaparte."]],
    )
    events = await collect(AgentLoop(router, registry_with(make_tool("find_entity", find_me))))
    assert isinstance(events[-2], DoneEvent)
    assert isinstance(events[-1], VerdictEvent) and events[-1].passed is False


async def test_verdict_rides_after_done_on_a_budget_stop() -> None:
    # A non-end_turn terminal (budget cap) still routes through the verdict tail. The
    # first (cheap) step surfaces the health source — touched_sensitive, non-empty
    # corpus — then the next step trips the budget cap mid-stream.
    router, _ = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("over budget", (ToolCall("c2", "search", {}),), "tool_use", LlmUsage(10, 10)),
        ],
        stream_chunks=[[""], ["the roof needs replacing"]],
    )
    loop = AgentLoop(
        router,
        registry_with(make_tool("search", search_cholesterol)),
        guardrails=Guardrails(max_cost_tokens=5),
    )
    events = await collect(loop, scopes=("health",))
    assert isinstance(events[-2], DoneEvent) and events[-2].stop_reason == "budget"
    assert isinstance(events[-1], VerdictEvent)


# --- Reflexion (Loop 1) opt-in buffer-then-retry, mode (a) -------------------


async def collect_buffered(
    loop: AgentLoop, scopes: tuple[str, ...] = ("general",)
) -> list[ChatEvent]:
    return [
        event
        async for event in loop.run_stream(
            session=OWNER,
            scopes=scopes,
            conversation=[UserMessage(text="what do I know?")],
            buffer_retry=True,
        )
    ]


async def test_buffer_retry_off_by_default_streams_live_with_no_retry() -> None:
    # The default path makes one streaming call; the buffered path is opt-in only.
    router, fake = stream_router_with(
        [LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["the roof needs replacing"]],
    )
    await collect(AgentLoop(router, registry_with(make_tool("search", search))), scopes=("health",))
    assert len(fake.stream_calls) == 1 and fake.converse_calls == []


async def test_buffer_retry_adopts_a_strictly_improving_reproduce() -> None:
    # mode (a): a critique-worthy turn whose first answer is ungrounded is
    # re-produced; the grounded retry strictly improves, so it is the one streamed.
    # Each produce runs the same tool (surfacing the cholesterol snippet); the
    # first answer ignores it (ungrounded), the second grounds in it.
    tool_use = LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1))
    router, fake = stream_router_with(
        [
            tool_use,
            LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1)),
            tool_use,
            LlmTurn("your cholesterol reading is elevated", (), "end_turn", LlmUsage(1, 1)),
        ],
    )
    events = await collect_buffered(
        AgentLoop(router, registry_with(make_tool("search", search_cholesterol)))
    )
    # The kept (second) attempt's text is what streamed; it grounds → no verdict.
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "your cholesterol reading is elevated"
    assert not any(isinstance(e, VerdictEvent) for e in events)
    # Re-produce happened via the non-streaming converse path (2 produce-steps,
    # 2 converse calls each).
    assert len(fake.converse_calls) == 4 and fake.stream_calls == []


async def test_buffer_retry_annotates_when_no_retry_improves() -> None:
    # Both attempts surface the cholesterol source (critique-worthy, non-empty
    # corpus) but stay ungrounded (no strict improvement) → the incumbent stands and
    # a verdict annotates it. The cap stops the loop; the user saw a spinner.
    tool_use = LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1))
    router, _ = stream_router_with(
        [
            tool_use,
            LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1)),
            tool_use,
            LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1)),
            tool_use,
            LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1)),
        ],
    )
    events = await collect_buffered(
        AgentLoop(router, registry_with(make_tool("search", search_cholesterol))),
        scopes=("health",),
    )
    assert isinstance(events[-1], VerdictEvent) and events[-1].passed is False


async def test_buffer_retry_non_critique_turn_does_not_retry() -> None:
    # A greeting in general scope is not critique-worthy → produced once, streamed,
    # no reflect loop and no verdict.
    router, fake = stream_router_with(
        [LlmTurn("hello there", (), "end_turn", LlmUsage(1, 1))],
    )
    events = await collect_buffered(AgentLoop(router, registry_with(make_tool("search", search))))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello there"
    assert not any(isinstance(e, VerdictEvent) for e in events)
    assert not any(isinstance(e, GeneralKnowledgeEvent) for e in events)  # a greeting: no label
    assert len(fake.converse_calls) == 1  # produced once, no retry


async def test_buffer_retry_zero_retrieval_substantive_turn_labels_general_knowledge() -> None:
    # The buffered path mirrors the live path's tail: a zero-retrieval substantive
    # answer (not critique-worthy, so produced once with no retry) still carries the
    # neutral general-knowledge label after done.
    router, fake = stream_router_with(
        [LlmTurn("Jeff is a short form of Jeffrey.", (), "end_turn", LlmUsage(1, 1))],
    )
    events = await collect_buffered(AgentLoop(router, registry_with(make_tool("search", search))))
    assert isinstance(events[-2], DoneEvent)
    assert isinstance(events[-1], GeneralKnowledgeEvent)
    assert not any(isinstance(e, VerdictEvent) for e in events)
    assert len(fake.converse_calls) == 1  # produced once, no retry


async def test_buffer_retry_replays_reasoning_of_the_kept_attempt() -> None:
    # The buffered (non-streaming) path emits the kept turn's reasoning as a single
    # ReasoningDelta ahead of the answer — the buffered twin of the live stream.
    router, _ = stream_router_with(
        [LlmTurn("the answer", (), "end_turn", LlmUsage(1, 1), reasoning="let me think")],
    )
    events = await collect_buffered(AgentLoop(router, registry_with(make_tool("search", search))))
    assert events[0] == ReasoningDelta(text="let me think")
    assert any(isinstance(e, TextDelta) and e.text == "the answer" for e in events)


async def test_buffer_retry_replays_buffered_tool_events_of_the_kept_attempt() -> None:
    # The buffered produce-step buffers tool_call / tool_result / view events; the
    # kept attempt's are replayed in order as the live stream (no discarded draft).
    router, _ = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "read_list", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("your cholesterol reading is elevated", (), "end_turn", LlmUsage(1, 1)),
        ],
    )
    events = await collect_buffered(
        AgentLoop(router, registry_with(make_tool("read_list", view_tool))), scopes=("health",)
    )
    types = [type(e).__name__ for e in events]
    # The buffered tool events stream in the same order the live path would emit.
    assert types.index("ToolCallEvent") < types.index("ToolResultEvent")
    assert types.index("ToolViewEvent") == types.index("ToolResultEvent") + 1
    assert "DoneEvent" in types


async def test_buffer_retry_respects_the_cost_guardrail_across_attempts() -> None:
    # A tiny per-turn budget: the first (cheap) step surfaces the sensitive source
    # (critique-worthy, non-empty corpus), then the next step exhausts the budget, so
    # the cost cap caps the retries (no unbounded reproduce). NOT the
    # self-improvement budget — the ordinary per-turn guardrail.
    router, fake = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn(
                "the roof needs replacing",
                (ToolCall("c2", "search", {}),),
                "tool_use",
                LlmUsage(10, 10),
            ),
        ],
    )
    loop = AgentLoop(
        router,
        registry_with(make_tool("search", search_cholesterol)),
        guardrails=Guardrails(max_cost_tokens=5),
    )
    events = await collect_buffered(loop, scopes=("health",))
    # The answer still streams + annotates, but the exhausted budget stopped any
    # reproduce after the first attempt — one produce-step (two converse calls), not
    # a retry loop.
    assert any(isinstance(e, VerdictEvent) for e in events)
    assert len(fake.converse_calls) == 2


async def test_buffer_retry_graph_answer_grounds_against_entity_aliases() -> None:
    # The buffered path threads entities through too: a graph answer grounds against
    # the entity aliases (critique-worthy via entity evidence, non-empty corpus), so
    # it passes on the first attempt — no retry, no verdict.
    router, fake = stream_router_with(
        [
            LlmTurn("", (ToolCall("c1", "find_entity", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("Your name is Jeffrey Mark Hopkins (Jeff).", (), "end_turn", LlmUsage(1, 1)),
        ],
    )
    events = await collect_buffered(
        AgentLoop(router, registry_with(make_tool("find_entity", find_me)))
    )
    assert not any(isinstance(e, VerdictEvent) for e in events)
    assert len(fake.converse_calls) == 2  # produced once, no retry


# --- shared tree budget (Wave S2) -------------------------------------------


async def test_run_charges_the_tree_and_a_child_stops_on_the_children_pool() -> None:
    # A child (depth >= 1) stops when total tree spend reaches the children's pool,
    # leaving the root reserve intact. Each model call here costs 20 tokens.
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(10, 10))]
    router, _ = router_with(forever)
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    tree = TreeState(tree_budget=100, root_reserve=25)  # children pool = 75
    result = await loop.run(
        session=OWNER,
        scopes=(),
        conversation=[UserMessage(text="hi")],
        depth=1,
        tree=tree,
    )
    assert result.stop_reason == "tree_budget_exhausted"
    assert tree.spent >= 75  # the children's pool
    assert tree.spent < 100  # but the child never touched the root's reserve


async def test_root_spends_the_whole_pool_including_its_reserve() -> None:
    # The root (depth 0) may spend the entire budget — only it can dip into the
    # reserve, so it can always run far enough to synthesize.
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(10, 10))]
    router, _ = router_with(forever)
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    tree = TreeState(tree_budget=100, root_reserve=25)
    result = await loop.run(
        session=OWNER,
        scopes=(),
        conversation=[UserMessage(text="hi")],
        depth=0,
        tree=tree,
    )
    assert result.stop_reason == "tree_budget_exhausted"
    assert tree.spent >= 100  # spent the reserve too


async def test_a_clean_end_turn_still_charges_the_tree_but_does_not_trip_the_budget() -> None:
    # end_turn is decided before the budget check, so a turn that finishes naturally
    # returns end_turn even with a tiny pool — and its one call is still charged.
    router, _ = router_with([LlmTurn("answer", (), "end_turn", LlmUsage(5, 5))])
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    tree = TreeState(tree_budget=4, root_reserve=1)
    result = await loop.run(
        session=OWNER,
        scopes=(),
        conversation=[UserMessage(text="hi")],
        depth=1,
        tree=tree,
    )
    assert result.stop_reason == "end_turn"
    assert tree.spent == 10


async def test_run_stream_charges_the_tree_and_stops_on_budget() -> None:
    # The streaming (root) path shares the same accounting as run().
    forever = [LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(10, 10))]
    router, _ = stream_router_with(forever)
    loop = AgentLoop(router, registry_with(make_tool("search", search)))
    tree = TreeState(tree_budget=100, root_reserve=25)
    events = [
        event
        async for event in loop.run_stream(
            session=OWNER,
            scopes=(),
            conversation=[UserMessage(text="hi")],
            depth=0,
            tree=tree,
        )
    ]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done and done[-1].stop_reason == "tree_budget_exhausted"
    assert tree.spent >= 100


# --- generalized live-event channel (Wave S2) -------------------------------


async def _event_emitting_tool(arguments: dict, ctx: ToolContext) -> str:
    # A tool whose work is itself a stream of events (the spawn handler's pattern)
    # pushes whole ChatEvents onto the turn's live channel.
    assert ctx.emit_event is not None
    ctx.emit_event(SubagentSpawnedEvent(child_id="ch1", persona="research", label="L", depth=1))
    return "fanned out"


async def test_run_stream_forwards_handler_events_with_the_call_id_injected() -> None:
    turns = [
        LlmTurn("", (ToolCall("c1", "fan", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = stream_router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("fan", _event_emitting_tool)))
    events = [
        event
        async for event in loop.run_stream(
            session=OWNER, scopes=(), conversation=[UserMessage(text="go")]
        )
    ]
    spawned = [e for e in events if isinstance(e, SubagentSpawnedEvent)]
    assert len(spawned) == 1
    assert spawned[0].child_id == "ch1"
    assert spawned[0].tool_call_id == "c1"  # the loop anchored it to the dispatching call
    # It streamed live — before the tool's own result event.
    s_idx = next(i for i, e in enumerate(events) if isinstance(e, SubagentSpawnedEvent))
    r_idx = next(i for i, e in enumerate(events) if isinstance(e, ToolResultEvent))
    assert s_idx < r_idx


async def test_batch_run_has_no_event_sink() -> None:
    # The non-streaming path (children run on it) has no live channel — emit_event is
    # None, so a grandchild's fan is not surfaced live (documented v1 limit).
    captured: list[ToolContext] = []

    async def _capture(arguments: dict, ctx: ToolContext) -> str:
        captured.append(ctx)
        return "ok"

    turns = [
        LlmTurn("", (ToolCall("c1", "probe", {}),), "tool_use", LlmUsage(1, 1)),
        LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
    ]
    router, _ = router_with(turns)
    loop = AgentLoop(router, registry_with(make_tool("probe", _capture)))
    await loop.run(session=OWNER, scopes=(), conversation=[UserMessage(text="go")])
    assert captured and captured[0].emit_event is None
