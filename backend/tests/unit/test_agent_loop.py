"""The agent loop: tool dispatch, the result feedback cycle, and every guardrail
(step / cost / consecutive-error caps), driven by the fake adapter and fake
tools — no real model, no database."""

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
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
    ToolSpec,
    ToolViewEvent,
    VerdictEvent,
    ViewPayload,
)
from jbrain.agent.loop import (
    SYSTEM_PROMPT,
    SYSTEM_VERSION,
    AgentLoop,
    Guardrails,
    JobRef,
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
        "agent-system-v4",
        "9d86df3adb7be857a153015a9da2aeb93a48eb17f1807651fa206e52efe61772",
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
