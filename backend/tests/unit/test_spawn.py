"""The sub-agent spawn service (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S1): the
structural caps and sandbox wiring, proven with NO model cooperation. Refusal paths
return before any DB/model touch; the success path uses fakes + a loop seam so the
service's wiring (clamp, depth, no-location, tree threading, lineage) is asserted
without a database or an LLM."""

import pytest

from jbrain.agent import spawn as spawn_mod
from jbrain.agent.agents import AGENTS, JERV_TOOLS
from jbrain.agent.briefs import FEED_OPEN
from jbrain.agent.loop import AgentResult, ToolContext
from jbrain.agent.spawn import SpawnService, effective_child_tools
from jbrain.agent.tree import (
    MAX_CHILDREN_PER_PARENT,
    MAX_DEPTH,
    MAX_PARALLEL,
    MAX_SUBFAN_PER_TASK_AGENT,
    MAX_WAVES,
    TreeState,
    child_steps_for,
)
from jbrain.db.session import SessionContext


def _review_brief(artifact: str = "the material") -> dict:
    """A minimal template-bound review brief — what a fed consumer must supply."""
    return {
        "template_id": "review",
        "params": {"artifact": artifact, "standard": "accuracy", "deliverable": "a table"},
    }


def _brief_text(call: dict) -> str:
    """The brief a recorded child run was launched with (2nd conversation message)."""
    return call["conversation"][1].text


# --- the clamp, as a pure function (parent⊆child) --------------------------


def test_clamp_intersects_persona_with_parent() -> None:
    parent = AGENTS["research"].tools or frozenset()
    assert effective_child_tools(AGENTS["research"].tools, JERV_TOOLS) == parent & JERV_TOOLS
    # A child never exceeds the parent, even for a tool its persona lists: a parent
    # missing web_fetch yields a child missing web_fetch.
    narrowed = frozenset({"web_search", "current_time"})
    child = effective_child_tools(AGENTS["research"].tools, narrowed)
    assert child <= narrowed
    assert "web_fetch" not in child
    # summarize (no persona tools) clamps to nothing regardless of the parent.
    assert effective_child_tools(AGENTS["summarize"].tools, JERV_TOOLS) == frozenset()


# --- fakes + a loop seam ----------------------------------------------------


class _FakeSessions:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, ctx, **kw):  # noqa: ANN001, ANN003
        self.created.append(kw)
        from datetime import UTC, datetime

        from jbrain.agent.session import AgentSessionInfo

        return AgentSessionInfo(
            id=f"sess-{len(self.created)}",
            title=kw.get("title", ""),
            status="active",
            domain_scopes=tuple(kw.get("domain_scopes", ())),
            subject_ids=(),
            created_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
            agent=kw.get("agent", "curator"),
        )


class _FakeRunLog:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.finished: list[dict] = []

    async def start(self, ctx, **kw):  # noqa: ANN001, ANN003
        self.started.append(kw)
        return f"run-{len(self.started)}"

    def bound(self, ctx, run_id):  # noqa: ANN001
        return object()

    async def finish(self, ctx, run_id, **kw):  # noqa: ANN001, ANN003
        self.finished.append({"run_id": run_id, **kw})


class _FakeRouter:
    """A non-local route by default, so the fan stays parallel; a test that wants the
    serial-on-local path sets `provider`."""

    def __init__(self, provider: str = "xai") -> None:
        self.provider = provider

    async def effective_reasoning_effort(self, task):  # noqa: ANN001
        return "high"

    async def effective_spec(self, task, strength=None):  # noqa: ANN001
        return (self.provider, "model-x")

    async def context_window(self, task):  # noqa: ANN001
        # The child's meter denominator — a fixed window for the fake.
        return 131_072


class _FakeLoop:
    """Records the kwargs each child run is launched with, so the service's wiring
    is observable without a real loop, DB, or model."""

    calls: list[dict] = []
    last_guardrails: object = None

    def __init__(self, *_a, **_k) -> None:
        _FakeLoop.last_guardrails = _k.get("guardrails")

    async def run(self, **kw):  # noqa: ANN003
        _FakeLoop.calls.append(kw)
        return AgentResult(
            text=f"summary for {kw['agent_session_id']}",
            stop_reason="end_turn",
            steps=1,
            cost_tokens=10,
        )


class _FakeTranscript:
    """Records each child exchange the service persists, so the rail-replay wiring is
    observable without a DB."""

    def __init__(self) -> None:
        self.exchanges: list[dict] = []

    async def record_exchange(
        self, ctx, *, session_id, run_id, user_text, assistant_text, tools, reasoning=""
    ):  # noqa: ANN001, ANN003, E501
        self.exchanges.append(
            {"session_id": session_id, "user_text": user_text, "assistant_text": assistant_text}
        )
        return "turn-1"


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> SpawnService:
    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _FakeLoop)
    return SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
    )


def _ctx(
    *, depth: int = 0, agent_tools: frozenset[str] = JERV_TOOLS, tree: TreeState | None = None
):
    return ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=depth,
        agent_tools=agent_tools,
        tree=tree if tree is not None else TreeState(),
        run_id="parent-run",
    )


# --- refusal paths (return before any mint; no model cooperation) -----------


async def test_a_child_cannot_spawn_children(service: SpawnService) -> None:
    """Nesting removed: only jerv (depth 0) may spawn — a child (depth == MAX_DEPTH == 1)
    is refused, so the tree is exactly two levels."""
    out = await service.spawn_fan(
        _ctx(depth=MAX_DEPTH), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls  # nothing launched


async def test_unknown_persona_refused_before_agent_for(service: SpawnService) -> None:
    out = await service.spawn_fan(
        _ctx(), {"tasks": [{"persona": "curator", "brief": "x", "label": "L"}]}
    )
    assert "refused" in out.lower() and "persona" in out.lower()
    assert not _FakeLoop.calls


async def test_empty_fan_refused(service: SpawnService) -> None:
    assert "refused" in (await service.spawn_fan(_ctx(), {"tasks": []})).lower()


async def test_over_large_fan_refused(service: SpawnService) -> None:
    tasks = [
        {"persona": "research", "brief": f"q{i}", "label": f"L{i}"}
        for i in range(MAX_CHILDREN_PER_PARENT + 1)
    ]
    out = await service.spawn_fan(_ctx(), {"tasks": tasks})
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


async def test_tree_total_cap_refused(service: SpawnService) -> None:
    tree = TreeState(agents_spawned=11, max_total_agents=12)  # only 1 slot left
    out = await service.spawn_fan(
        _ctx(tree=tree),
        {
            "tasks": [
                {"persona": "research", "brief": "a", "label": "A"},
                {"persona": "research", "brief": "b", "label": "B"},
            ]
        },
    )
    assert "refused" in out.lower()
    assert tree.agents_spawned == 11  # not admitted


async def test_budget_admission_floor_refuses_when_pool_too_low(service: SpawnService) -> None:
    """Wave S2: a fan is refused when the children's pool can't seat a minimum viable
    slice per child, even if the structural total-agents cap has room."""
    tree = TreeState(tree_budget=200_000, root_reserve=50_000)  # children pool 150k
    tree.charge(100_000)  # children_remaining now 50k < 100k floor
    out = await service.spawn_fan(
        _ctx(tree=tree), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "refused" in out.lower() and "budget" in out.lower()
    assert not _FakeLoop.calls
    assert tree.agents_spawned == 0  # not admitted


async def test_spawn_refused_without_a_tree_pool(service: SpawnService) -> None:
    """Fail closed: a caller that never seeded a tree (e.g. the scheduled task
    runner) cannot spawn — the total-agents cap would otherwise be defeated by a
    throwaway per-call counter. Only an interactive owner turn seeds a tree."""
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="s",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=None,
        run_id="r",
    )
    out = await service.spawn_fan(
        ctx, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


def _capturing_ctx(captured: list) -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )


async def test_capped_child_with_a_synthesized_answer_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child cut off on budget/steps that STILL synthesized a real answer is complete-
    but-deep, NOT truncated: it counts as ok, its summary is the answer (no '[truncated]'
    tag), and the synthesis card carries truncated=False. Truncation is now reserved for a
    capped child that produced nothing (see test_degraded_child_is_flagged_failed)."""

    class _CappedLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            return AgentResult(
                text="partial findings", stop_reason="budget", steps=4, cost_tokens=9
            )

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _CappedLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    captured: list = []
    out = await svc.spawn_fan(
        _capturing_ctx(captured), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "partial findings" in out
    assert "[FAILED]" not in out  # it is ok (has content)
    assert "truncated" not in out.lower()  # no alarming suffix in the parent observation
    view = [e for e in captured if e.type == "tool_view"][-1]
    assert view.view.data["truncated"] is False  # complete-but-deep, not a red ✕


async def test_step_limited_child_with_a_forced_answer_is_ok_and_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that hits max_steps but synthesized a final answer (force_final_answer)
    counts as ok and is NOT flagged truncated — it has real content. The child loop is
    launched with force_final_answer set."""

    class _StepLimitedLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            return AgentResult(
                text="best-effort findings", stop_reason="max_steps", steps=10, cost_tokens=9
            )

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _StepLimitedLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    captured: list = []
    out = await svc.spawn_fan(
        _capturing_ctx(captured), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "best-effort findings" in out
    assert "[FAILED]" not in out
    assert _FakeLoop.calls[0]["force_final_answer"] is True
    view = [e for e in captured if e.type == "tool_view"][-1]
    assert view.view.data["truncated"] is False


async def test_degraded_child_is_flagged_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that stops without a substantive answer (max_steps / empty text) is
    surfaced as [FAILED], not folded in as a clean summary."""

    class _EmptyLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            return AgentResult(text="", stop_reason="max_steps", steps=10, cost_tokens=5)

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _EmptyLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    out = await svc.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "[FAILED]" in out
    assert "1 failed" in out


# --- success path: the wiring is structural --------------------------------


async def test_fan_mints_clamped_sandboxed_children_in_order(service: SpawnService) -> None:
    sessions: _FakeSessions = service._sessions  # type: ignore[assignment]
    tree = TreeState()
    out = await service.spawn_fan(
        _ctx(tree=tree),
        {
            "tasks": [
                {"persona": "research", "brief": "first", "label": "Alpha"},
                {"persona": "summarize", "brief": "second", "label": "Beta"},
            ]
        },
    )
    # Both children minted with the sub-agent lineage + sandbox flag.
    assert len(sessions.created) == 2
    for created in sessions.created:
        assert created["parent_session_id"] == "parent-sess"
        assert created["depth"] == 1
        assert created["no_memory"] is True
    assert tree.agents_spawned == 2

    # Each child loop launched clamped, sandboxed, at depth 1, sharing the tree.
    by_session = {c["agent_session_id"]: c for c in _FakeLoop.calls}
    research_call = next(c for c in _FakeLoop.calls if c["system"] == AGENTS["research"].prompt)
    assert research_call["tools_allow"] == effective_child_tools(
        AGENTS["research"].tools, JERV_TOOLS
    )
    assert research_call["depth"] == 1
    assert research_call["scopes"] == ()
    assert research_call["tree"] is tree
    # No location is ever passed to a child (M2) — `here` stays unset → None.
    assert "here" not in research_call or research_call.get("here") is None
    summarize_call = next(c for c in _FakeLoop.calls if c["system"] == AGENTS["summarize"].prompt)
    assert summarize_call["tools_allow"] == frozenset()  # summarize holds no tools

    # Stable label order in the folded observation.
    assert out.index("Alpha") < out.index("Beta")
    assert "2 ran" in out
    assert len(by_session) == 2


async def test_child_effort_is_threaded_to_the_loop(service: SpawnService) -> None:
    """The spawner's per-child `effort` reaches the child loop as `reasoning_effort`
    (the router drops it for a non-reasoning child model); an omitted effort is None,
    leaving the child model's resolved default."""
    await service.spawn_fan(
        _ctx(),
        {
            "tasks": [
                {"persona": "research", "brief": "a", "label": "Hard", "effort": "high"},
                {"persona": "research", "brief": "b", "label": "Plain"},
            ]
        },
    )
    by_label = {c["agent_session_id"]: c for c in _FakeLoop.calls}
    calls = list(by_label.values())
    hard = next(c for c in calls if c["conversation"][-1].text == "a")
    plain = next(c for c in calls if c["conversation"][-1].text == "b")
    assert hard["reasoning_effort"] == "high"
    assert plain["reasoning_effort"] is None


async def test_local_route_serializes_the_fan() -> None:
    """A single-GPU local route forces concurrency to 1 (serial) — a parallel fan
    would split the one device and push each child past its wall-clock. A non-local
    route keeps the model-requested concurrency (clamped)."""

    def _svc(provider: str) -> SpawnService:
        return SpawnService(
            router=_FakeRouter(provider),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            sessions=_FakeSessions(),  # type: ignore[arg-type]
            runlog=_FakeRunLog(),  # type: ignore[arg-type]
        )

    assert await _svc("local")._effective_max_parallel(4) == 1
    assert await _svc("local")._effective_max_parallel(None) == 1
    # A cloud route keeps the requested concurrency, still clamped to MAX_PARALLEL.
    assert await _svc("xai")._effective_max_parallel(2) == 2
    assert await _svc("xai")._effective_max_parallel(99) == MAX_PARALLEL


async def test_child_brief_and_answer_persisted_to_its_own_transcript(
    service: SpawnService,
) -> None:
    """Opening a sub-agent in the sessions rail must replay its work, not an empty
    chat — so each child's brief→answer is recorded to the CHILD's own transcript."""
    await service.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "what is HNSW?", "label": "HNSW"}]}
    )
    tx: _FakeTranscript = service._transcript  # type: ignore[assignment]
    assert len(tx.exchanges) == 1
    ex = tx.exchanges[0]
    assert ex["session_id"] == "sess-1"  # the child session, not "parent-sess"
    assert ex["user_text"] == "what is HNSW?"
    assert ex["assistant_text"] == "summary for sess-1"


async def test_high_effort_child_gets_a_larger_step_cap(service: SpawnService) -> None:
    """A high-effort child is launched with the effort-scaled step cap so it can do
    thorough research before the cap bites; a default child gets the base cap."""
    await service.spawn_fan(
        _ctx(),
        {"tasks": [{"persona": "research", "brief": "x", "label": "L", "effort": "high"}]},
    )
    assert _FakeLoop.last_guardrails is not None
    assert _FakeLoop.last_guardrails.max_steps == child_steps_for("high")  # type: ignore[attr-defined]

    await service.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "y", "label": "M"}]}
    )
    assert _FakeLoop.last_guardrails.max_steps == child_steps_for(None)  # type: ignore[attr-defined]


async def test_unknown_effort_refuses_the_fan(service: SpawnService) -> None:
    out = await service.spawn_fan(
        _ctx(),
        {"tasks": [{"persona": "research", "brief": "x", "label": "L", "effort": "ludicrous"}]},
    )
    assert "refused" in out.lower() and "effort" in out.lower()
    assert not _FakeLoop.calls


async def test_fan_emits_subagent_lifecycle_events(service: SpawnService) -> None:
    """The fan streams spawned → progress → done per child onto the parent turn's
    event sink (Wave S2). These are the backend-authored frames the in-chat accordion
    (Wave S3) will render; ephemeral, never persisted."""
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    await service.spawn_fan(ctx, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    # spawned → progress → done per child, then the incremental roster-so-far view emitted
    # as the child settles (so a fan cut short still persists what finished — the loop
    # stamps the spawn call id and the live UI suppresses it under the live fan).
    assert [e.type for e in captured] == [
        "subagent_spawned",
        "subagent_progress",
        "subagent_done",
        "tool_view",
    ]
    spawned, progress, done, view = captured
    assert (spawned.persona, spawned.label, spawned.depth) == ("research", "L", 1)
    assert spawned.child_id == "sess-1"
    assert progress.phase == "researching"
    assert done.ok is True and done.child_id == "sess-1"
    assert view.view.view == "subagent_synthesis" and view.tool_call_id == ""


async def test_fan_emits_incremental_synthesis_view_as_each_child_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As EACH child settles, the fan re-emits the roster-so-far as a `tool_view` (the
    incremental synthesis). So a two-child fan emits TWO views — the first carrying one
    child, the second carrying both — and the loop stamps the spawn call id (the event
    rides tool_call_id=""). This is what lets a fan cut short persist the children that
    had finished: the last view folded onto the spawn step is the durable roster."""
    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _FakeLoop)
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    await svc.spawn_fan(
        ctx,
        {
            "tasks": [
                {"persona": "research", "brief": "a", "label": "A"},
                {"persona": "research", "brief": "b", "label": "B"},
            ]
        },
    )
    views = [e for e in captured if e.type == "tool_view"]
    # One incremental view per settled child — the roster grows 1 → 2.
    assert len(views) == 2
    assert all(v.tool_call_id == "" for v in views)  # the loop stamps the spawn call id
    assert all(v.view.view == "subagent_synthesis" for v in views)
    assert views[0].view.data["ran"] == 1
    assert views[-1].view.data["ran"] == 2
    assert {c["label"] for c in views[-1].view.data["children"]} == {"A", "B"}


async def test_cut_short_fan_persists_the_children_that_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix #2: a fan cancelled (Stop / error / turn_timeout) AFTER k children settle still
    leaves a partial `subagent_synthesis` roster of those k — so a reopened transcript
    shows the research surface instead of a bare spawn row. We model the parent turn's fold
    exactly: feed the spawn tool_call + the incremental view the fan emitted for the one
    child that finished (the loop stamps it with the spawn call id), then the synthetic
    terminal `done` a cut-short turn settles with. The accumulator must carry that view on
    the spawn step (no `tool_result` ever arrives), and `tool_steps()` is what gets
    persisted."""
    from jbrain.agent.contracts import DoneEvent, TextDelta, ToolCallEvent, ToolViewEvent
    from jbrain.agent.spawn import _ChildResult, _synthesis_view
    from jbrain.agent.transcript_accumulator import TranscriptAccumulator

    # The first child finished before the Stop; the fan emitted its roster-so-far view.
    settled_view = _synthesis_view(
        [
            _ChildResult(
                label="A", persona="research", summary="found x", ok=True, session_id="sess-A"
            )
        ]
    )

    acc = TranscriptAccumulator()
    acc.feed(TextDelta(text="Spinning up researchers…"))
    acc.feed(ToolCallEvent(id="call-1", name="spawn_subagent", arguments={"tasks": []}))
    # The incremental view, loop-stamped with the spawn call id — folds onto the step
    # even though no tool_result followed (the fan never reached its final ToolOutput).
    acc.feed(ToolViewEvent(tool_call_id="call-1", view=settled_view))
    acc.feed(DoneEvent(stop_reason="turn_timeout"))  # the synthetic terminal of a cut turn

    spawn_step = next(s for s in acc.tool_steps() if s["name"] == "spawn_subagent")
    # The partial roster survives on the spawn step — the reopened transcript shows it.
    assert spawn_step["view"]["view"] == "subagent_synthesis"
    assert spawn_step["view"]["data"]["ran"] == 1
    children = spawn_step["view"]["data"]["children"]
    assert [c["label"] for c in children] == ["A"]
    # The child's session id rides the view so the card row can deep-link to it on reopen.
    assert children[0]["session_id"] == "sess-A"


async def test_all_children_announced_up_front_as_queued(service: SpawnService) -> None:
    """Every child is minted + announced (subagent_spawned) BEFORE any starts running,
    so the whole roster shows at once — the not-yet-started ones as "queued" — even when
    the fan is serialized. Each flips to its working phase via a later progress event."""
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    await service.spawn_fan(
        ctx,
        {
            "tasks": [
                {"persona": "research", "brief": "a", "label": "A"},
                {"persona": "research", "brief": "b", "label": "B"},
            ]
        },
    )
    # Both spawned events lead, before any progress (the roster, all queued).
    assert [e.type for e in captured[:2]] == ["subagent_spawned", "subagent_spawned"]
    assert {e.label for e in captured[:2]} == {"A", "B"}
    # Working phases follow, once each child actually starts.
    assert "subagent_progress" in [e.type for e in captured[2:]]


async def test_child_streams_live_deltas_to_the_fan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child's loop streams its tokens via on_text/on_reasoning; the service forwards
    them as subagent_delta events (tagged by child_id) so the fan shows it working."""

    class _StreamingLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            kw["on_reasoning"]("let me search…")
            kw["on_text"]("Port St. John summary")
            return AgentResult(
                text="Port St. John summary", stop_reason="end_turn", steps=1, cost_tokens=5
            )

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _StreamingLoop)
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    await svc.spawn_fan(ctx, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    deltas = [e for e in captured if e.type == "subagent_delta"]
    assert any(d.channel == "reasoning" and d.text == "let me search…" for d in deltas)
    assert any(d.channel == "answer" and d.text == "Port St. John summary" for d in deltas)
    assert all(d.child_id == "sess-1" for d in deltas)


async def test_child_usage_forwarded_as_context_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child's per-call usage (on_usage) is forwarded as a subagent_usage event — its
    fill over the child model's window — so the fan row shows a live context meter."""

    class _UsageLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            kw["on_usage"](12_000, 800)  # the latest model call's prompt + output
            return AgentResult(text="ok", stop_reason="end_turn", steps=1, cost_tokens=5)

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _UsageLoop)
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    await svc.spawn_fan(ctx, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    usage = [e for e in captured if e.type == "subagent_usage"]
    assert len(usage) == 1
    assert usage[0].child_id == "sess-1"
    assert usage[0].used == 12_800  # prompt + output
    assert usage[0].context_window == 131_072  # from the (fake) router


async def test_child_tool_steps_forwarded_to_the_fan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child's tool calls are forwarded as subagent_tool events with a short inline arg
    (the query/url), so the fan frame shows its work like a real session."""

    class _ToolingLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            kw["on_tool"]("web_search", {"query": "port saint john news"}, True)
            kw["on_tool"]("web_fetch", {"url": "https://floridatoday.test/x"}, False)
            return AgentResult(text="ok", stop_reason="end_turn", steps=2, cost_tokens=5)

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _ToolingLoop)
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    await svc.spawn_fan(ctx, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    tools = [e for e in captured if e.type == "subagent_tool"]
    assert any(t.name == "web_search" and t.arg == "port saint john news" and t.ok for t in tools)
    assert any(
        t.name == "web_fetch" and t.arg == "https://floridatoday.test/x" and not t.ok for t in tools
    )


async def test_fan_without_a_sink_does_not_emit(service: SpawnService) -> None:
    # A turn with no event sink simply skips live emission — no crash; the fan still
    # runs and folds its summary.
    out = await service.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "research" in out.lower()  # the fan still ran and folded its summary


async def test_live_progress_streams_per_child_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each child ReAct step fires on_step → a subagent_progress carrying the step and
    the live tree snapshot, so the UI's meter + step count move while a (non-streaming)
    child works."""

    class _SteppingLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            on_step = kw.get("on_step")
            assert on_step is not None
            on_step(1, 1000)
            on_step(2, 2000)
            return AgentResult(text="done", stop_reason="end_turn", steps=2, cost_tokens=2000)

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _SteppingLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    captured: list = []
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id="parent-run",
        emit_event=captured.append,
    )
    await svc.spawn_fan(ctx, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    steps = sorted(e.step for e in captured if e.type == "subagent_progress")
    assert steps == [0, 1, 2]  # the spawn-time tick (0) + one per ReAct step


async def test_child_wall_clock_degrades_a_slow_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child past its wall-clock is cancelled and degraded (timeout), so one slow
    child can't stall the fan."""
    import asyncio

    class _SlowLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            await asyncio.sleep(1.0)  # longer than the patched wall-clock below
            return AgentResult(text="late", stop_reason="end_turn", steps=1, cost_tokens=1)

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "CHILD_WALL_CLOCK_S", 0.05)
    monkeypatch.setattr(spawn_mod, "AgentLoop", _SlowLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    out = await svc.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "timed out" in out.lower()
    assert "[FAILED]" in out


async def test_stop_cascades_cancellation_into_the_fan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The composer Stop cancels the parent turn; that CancelledError propagates into
    spawn_fan's gather and cancels every in-flight child — the whole tree halts and the
    child's run is marked cancelled, not left running."""
    import asyncio

    started = asyncio.Event()

    class _BlockingLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            started.set()
            await asyncio.sleep(3600)  # block until the parent cancel cascades in
            return AgentResult(text="", stop_reason="end_turn", steps=0, cost_tokens=0)  # unreached

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _BlockingLoop)
    runlog = _FakeRunLog()
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=runlog,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(
        svc.spawn_fan(_ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    )
    await started.wait()  # the child is mid-run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The child didn't keep running — its run was finished as cancelled.
    assert any(f["stop_reason"] == "cancelled" for f in runlog.finished)


async def test_cancelled_child_runlog_settles_inline_not_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled fan settles each child's run-log to 'cancelled' (never stranded
    'running') even when finish is a real DB round-trip that YIELDS mid-cleanup —
    asyncio.gather awaits the children's cancellation cleanup before propagating, so the
    write lands inline. Guards against a regression to fire-and-forget children (e.g. a
    bare create_task) that would abandon the close. The yielding finish is the teeth: a
    detached cleanup wouldn't complete before the fan task unwinds."""
    import asyncio

    started = asyncio.Event()

    class _BlockingLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            started.set()
            await asyncio.sleep(3600)  # block until the parent cancel cascades in
            return AgentResult(text="", stop_reason="end_turn", steps=0, cost_tokens=0)  # unreached

    class _YieldingRunLog(_FakeRunLog):
        async def finish(self, ctx, run_id, **kw):  # noqa: ANN001, ANN003
            await asyncio.sleep(0)  # a real finish yields mid-cleanup (a DB round-trip)
            self.finished.append({"run_id": run_id, **kw})

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _BlockingLoop)
    runlog = _YieldingRunLog()
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=runlog,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(
        svc.spawn_fan(_ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]})
    )
    await started.wait()  # the child is mid-run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The child's cancelled run-log close completed INLINE (awaited by the fan), not
    # abandoned mid-yield — so the row settles 'cancelled' instead of stranding 'running'.
    assert any(f["stop_reason"] == "cancelled" for f in runlog.finished)


# --- feeding waves: the staged pipeline (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md) ---


async def test_waves_feed_producer_summary_into_consumer(service: SpawnService) -> None:
    """A wave-2 consumer's brief carries the wave-1 producer's summary, wrapped in the
    data boundary — and the producer, being un-fed, does not."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [{"persona": "research", "brief": "fetch the commit history", "label": "prod"}],
                [
                    {
                        "persona": "review",
                        "label": "cons",
                        "feed": ["prod"],
                        "brief": _review_brief(),
                    }
                ],
            ]
        },
    )
    assert len(_FakeLoop.calls) == 2  # producer, then consumer (barrier between waves)
    prod_brief, cons_brief = _brief_text(_FakeLoop.calls[0]), _brief_text(_FakeLoop.calls[1])
    assert FEED_OPEN not in prod_brief  # producer is un-fed
    assert FEED_OPEN in cons_brief  # consumer got the boundary-wrapped feed …
    assert "summary for sess-1" in cons_brief  # … containing the producer's summary
    assert "2 ran" in out
    # The synthesis view groups by wave and carries the feed edge for F3's surface.
    children = {c["label"]: c for c in out.view.data["children"]}  # type: ignore[attr-defined]
    assert children["prod"]["wave"] == 0 and children["prod"]["fed_from"] == []
    assert children["cons"]["wave"] == 1 and children["cons"]["fed_from"] == ["prod"]


async def test_waves_skip_consumer_when_producer_fails(
    service: SpawnService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed cascade: a producer that returns no usable answer (ok=False) leaves
    its consumer SKIPPED — never run over an empty/error block — surfaced distinctly."""

    class _EmptyProducer(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            # The producer's free-text brief mentions "fetch"; it returns empty (a
            # failure). The consumer, if it ran, would have a template brief instead.
            text = "" if "fetch" in kw["conversation"][1].text else "review done"
            return AgentResult(text=text, stop_reason="end_turn", steps=1, cost_tokens=10)

    monkeypatch.setattr(spawn_mod, "AgentLoop", _EmptyProducer)
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [{"persona": "research", "brief": "fetch history", "label": "prod"}],
                [
                    {
                        "persona": "review",
                        "label": "cons",
                        "feed": ["prod"],
                        "brief": _review_brief(),
                    }
                ],
            ]
        },
    )
    assert len(_FakeLoop.calls) == 1  # only the producer ran; the consumer was skipped
    assert "[SKIPPED" in out and "cons" in out
    assert "1 skipped" in out


async def test_waves_fed_consumer_must_be_template_bound(service: SpawnService) -> None:
    """A fed consumer with a free-text brief is refused — the fed data must land in a
    data-framed template slot (the review's depth-0 concern)."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [{"persona": "research", "brief": "fetch", "label": "p"}],
                [{"persona": "review", "label": "c", "feed": ["p"], "brief": "free text"}],
            ]
        },
    )
    assert "refused" in out.lower() and "template-bound" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_feed_must_reference_an_earlier_wave(service: SpawnService) -> None:
    """A consumer may only feed from a strictly earlier wave — a same-wave feed edge is
    refused (no intra-wave ordering, no cycles)."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [
                    {"persona": "research", "brief": "a", "label": "a"},
                    {"persona": "review", "label": "b", "feed": ["a"], "brief": _review_brief()},
                ]
            ]
        },
    )
    assert "refused" in out.lower() and "earlier wave" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_sibling_reference_without_feed_refused(service: SpawnService) -> None:
    """The primary structural fix: a brief that refers to data it never received (a
    guard phrase) is refused instead of running empty."""
    out = await service.spawn_fan(
        _ctx(),
        {"waves": [[{"persona": "research", "brief": "use the same commit list", "label": "p"}]]},
    )
    assert "refused" in out.lower() and "feed" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_reference_to_unfed_sibling_label_refused(service: SpawnService) -> None:
    """Naming another task's label without a `feed` edge is caught too (word-boundary)."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [{"persona": "research", "brief": "gather commits", "label": "history"}],
                [
                    {
                        "persona": "review",
                        "label": "c",
                        "brief": "assess the history output",  # names 'history', no feed
                    }
                ],
            ]
        },
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_refused_when_nested(service: SpawnService) -> None:
    """Staged waves are a top-level (depth-0) capability; a depth>=1 caller is refused
    (and a child cannot spawn at all now — belt and suspenders)."""
    out = await service.spawn_fan(
        _ctx(depth=1),
        {"waves": [[{"persona": "research", "brief": "x", "label": "p"}]]},
    )
    assert "refused" in out.lower() and "top-level" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_over_max_refused(service: SpawnService) -> None:
    waves = [
        [{"persona": "research", "brief": f"q{i}", "label": f"L{i}"}] for i in range(MAX_WAVES + 1)
    ]
    out = await service.spawn_fan(_ctx(), {"waves": waves})
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_total_children_cap(service: SpawnService) -> None:
    """The whole staged call still obeys the per-fan child cap across all waves."""
    wave1 = [
        {"persona": "research", "brief": f"q{i}", "label": f"A{i}"}
        for i in range(MAX_CHILDREN_PER_PARENT)
    ]
    wave2 = [{"persona": "research", "brief": "one more", "label": "B0"}]
    out = await service.spawn_fan(_ctx(), {"waves": [wave1, wave2]})
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_duplicate_label_refused(service: SpawnService) -> None:
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [{"persona": "research", "brief": "a", "label": "dup"}],
                [{"persona": "review", "label": "dup", "feed": [], "brief": "b"}],
            ]
        },
    )
    assert "refused" in out.lower() and "unique" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_fed_consumer_referencing_unfed_sibling_refused(service: SpawnService) -> None:
    """F1 review fix: the sibling guard must NOT be bypassed for a fed consumer. A
    consumer that feeds `alpha` but whose brief also names the un-fed `beta` is refused
    — otherwise it runs empty against beta (the exact foot-gun the guard exists for)."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [
                    {"persona": "research", "brief": "get alpha", "label": "alpha"},
                    {"persona": "research", "brief": "get beta", "label": "beta"},
                ],
                [
                    {
                        "persona": "review",
                        "label": "cons",
                        "feed": ["alpha"],
                        "brief": _review_brief("combine alpha with the beta findings"),
                    }
                ],
            ]
        },
    )
    assert "refused" in out.lower() and "beta" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_guard_ignores_template_scaffolding_words(service: SpawnService) -> None:
    """F2 review fix: the sibling guard scans model-supplied param VALUES, not the fixed
    template scaffolding — so a producer labelled a common template word ("summary")
    must not spuriously refuse an unrelated fed consumer."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [
                    {"persona": "research", "brief": "get commits", "label": "summary"},
                    {"persona": "research", "brief": "get trends", "label": "trends"},
                ],
                [
                    {
                        "persona": "review",
                        "label": "cons",
                        "feed": ["trends"],
                        "brief": _review_brief("the trend data"),  # does not name 'summary'
                    }
                ],
            ]
        },
    )
    # 'summary' is a wave-1 label AND a word in the rendered review template, but the
    # consumer's own params never mention it → it runs, not refused.
    assert "refused" not in out.lower()
    assert len(_FakeLoop.calls) == 3


async def test_waves_same_wave_reference_gets_move_guidance(service: SpawnService) -> None:
    """F2 review fix: a brief naming a SAME-wave sibling (which cannot be fed) is refused
    with actionable 'move to an earlier wave' guidance, never a dead-end 'add a feed
    edge' (a same-wave feed is itself refused)."""
    out = await service.spawn_fan(
        _ctx(),
        {
            "waves": [
                [
                    {"persona": "research", "brief": "get the history", "label": "history"},
                    {"persona": "review", "brief": "assess the history data", "label": "auditor"},
                ]
            ]
        },
    )
    assert "refused" in out.lower() and "move" in out.lower() and "earlier wave" in out.lower()
    assert not _FakeLoop.calls


async def test_both_tasks_and_waves_refused(service: SpawnService) -> None:
    out = await service.spawn_fan(
        _ctx(),
        {
            "tasks": [{"persona": "research", "brief": "x", "label": "L"}],
            "waves": [[{"persona": "research", "brief": "y", "label": "M"}]],
        },
    )
    assert "refused" in out.lower() and "both" in out.lower()
    assert not _FakeLoop.calls


async def test_waves_deadline_skips_loud(service: SpawnService) -> None:
    """F2: a staged call past its tree wall-clock deadline skips every wave loudly,
    running nothing."""
    import time

    tree = TreeState(deadline=time.monotonic() - 1)  # already past
    out = await service.spawn_fan(
        _ctx(tree=tree),
        {
            "waves": [
                [{"persona": "research", "brief": "fetch", "label": "p"}],
                [{"persona": "review", "label": "c", "feed": ["p"], "brief": _review_brief()}],
            ]
        },
    )
    assert not _FakeLoop.calls
    assert "deadline" in out.lower()


async def test_waves_budget_skip_of_final_wave(
    service: SpawnService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: when an earlier wave drains the shared pool, the final wave is budget-skipped
    loud (never silently dropped) — the producer ran, the consumer did not."""

    class _Spender(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            kw["tree"].charge(450_000)  # the producer burns most of the pool
            return AgentResult(
                text=f"summary for {kw['agent_session_id']}",
                stop_reason="end_turn",
                steps=1,
                cost_tokens=10,
            )

    monkeypatch.setattr(spawn_mod, "AgentLoop", _Spender)
    tree = TreeState(tree_budget=500_000, root_reserve=0)  # children pool 500k, no deadline
    out = await service.spawn_fan(
        _ctx(tree=tree),
        {
            "waves": [
                [{"persona": "research", "brief": "fetch", "label": "p"}],
                [{"persona": "review", "label": "c", "feed": ["p"], "brief": _review_brief()}],
            ]
        },
    )
    assert len(_FakeLoop.calls) == 1  # producer ran; consumer budget-skipped
    assert "budget" in out.lower() and "[SKIPPED" in out


async def test_barrier_cancel_settles_wave1_and_wave2_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop during wave 2 must leave wave 1 settled (done, not stranded 'running') and
    wave 2's in-flight child settled 'cancelled' inline — the barrier composes with the
    per-wave gather's cancel-and-await cleanup."""
    import asyncio

    started = asyncio.Event()

    class _Block2(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            if "fetch" in kw["conversation"][1].text:  # wave-1 producer → succeed
                return AgentResult(
                    text=f"summary for {kw['agent_session_id']}",
                    stop_reason="end_turn",
                    steps=1,
                    cost_tokens=10,
                )
            started.set()  # wave-2 consumer → block until the cancel cascades
            await asyncio.sleep(3600)
            return AgentResult(text="", stop_reason="end_turn", steps=0, cost_tokens=0)

    class _YieldingRunLog(_FakeRunLog):
        async def finish(self, ctx, run_id, **kw):  # noqa: ANN001, ANN003
            await asyncio.sleep(0)
            self.finished.append({"run_id": run_id, **kw})

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _Block2)
    runlog = _YieldingRunLog()
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=runlog,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(
        svc.spawn_fan(
            _ctx(),
            {
                "waves": [
                    [{"persona": "research", "brief": "fetch", "label": "p"}],
                    [{"persona": "review", "label": "c", "feed": ["p"], "brief": _review_brief()}],
                ]
            },
        )
    )
    await started.wait()  # wave 1 done, wave-2 consumer mid-run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(f["stop_reason"] == "end_turn" for f in runlog.finished)  # wave 1 settled done
    assert any(f["stop_reason"] == "cancelled" for f in runlog.finished)  # wave 2 settled inline


def test_synthesis_view_separates_skipped_from_ran_and_failed() -> None:
    """F1 review fix: a skipped consumer must not inflate `ran` or count as `failed`."""
    from jbrain.agent.spawn import _ChildResult, _synthesis_view

    results = [
        _ChildResult("a", "research", "ok", ok=True, session_id="s1"),
        _ChildResult(
            "b",
            "review",
            "(skipped — upstream a unavailable)",
            ok=False,
            session_id="",
            skipped="upstream a unavailable",
        ),
    ]
    view = _synthesis_view(results)
    assert view.data["ran"] == 1
    assert view.data["failed"] == 0
    assert view.data["skipped"] == 1
    child_b = view.data["children"][1]
    assert child_b["skipped"] is True and child_b["skip_reason"] == "upstream a unavailable"


def test_tool_arg_previews_the_human_readable_target_not_opaque_ids():
    """A child tool step's inline preview surfaces the query/url/name/place it ran —
    matching the frontend INLINE_ARG_KEY — and stays empty for id-only tools."""
    from jbrain.agent.spawn import _TOOL_ARG_LEN, _tool_arg

    assert _tool_arg("web_search", {"query": "rust"}) == "rust"
    assert _tool_arg("web_fetch", {"url": " https://x.example/a "}) == "https://x.example/a"
    assert _tool_arg("gmail_search", {"query": "from:wellsfargo", "limit": 20}) == "from:wellsfargo"
    assert _tool_arg("find_entity", {"name": "Celine"}) == "Celine"
    assert _tool_arg("where_is", {"subject": "Jeff"}) == "Jeff"
    # Opaque-id tools (message_id, note_id, …) carry no legible preview.
    assert _tool_arg("gmail_read", {"message_id": "abc123"}) == ""
    assert _tool_arg("read_note", {"note_id": "n1"}) == ""
    # A non-string arg or a non-dict payload degrades to empty, never raises.
    assert _tool_arg("web_search", {"query": 7}) == ""
    assert _tool_arg("web_search", "not-a-dict") == ""
    # Long previews are clamped so one call can't blow out the fan row.
    assert len(_tool_arg("web_search", {"query": "x" * 500})) == _TOOL_ARG_LEN


class _SlowLoop:
    """A child loop that never returns on its own — so only a wall-clock cut ends it."""

    def __init__(self, *_a, **_k) -> None:  # noqa: ANN002, ANN003
        pass

    async def run(self, **kw):  # noqa: ANN003
        import asyncio

        await asyncio.sleep(30)
        raise AssertionError("child should have been cut by the wall-clock")  # pragma: no cover


async def test_flat_fan_children_honor_the_tree_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flat fan is bounded by the tree wall-clock, not only the per-child clock: once
    the tree deadline has passed, each child is cut with a timeout result instead of
    running on to CHILD_WALL_CLOCK_S — the runaway a stalled fan (e.g. one hammering
    blocked directory sites) used to hit, since a flat fan never consulted the deadline."""
    import time

    monkeypatch.setattr(spawn_mod, "AgentLoop", _SlowLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
    )
    tree = TreeState.rooted(1_000_000)
    tree.deadline = time.monotonic() + 0.05  # essentially out of time already
    out = await svc.spawn_fan(
        _ctx(tree=tree), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "timed out" in out.lower()


async def test_a_task_agent_spawns_one_tier_when_the_run_allows_depth_2(
    service: SpawnService,
) -> None:
    """Run-scoped depth (deepest-research R2): a tree seeded `max_depth=2` lets a depth-1
    task agent spawn a tier of sub agents — orchestrator → task agent → sub agent. The
    sub agent lands at depth 2; that it is itself a leaf is the default-tree refusal in
    `test_a_child_cannot_spawn_children`. Ordinary runs (default `max_depth=1`) are
    unaffected — the depth change is confined to the run that opts in."""
    tree = TreeState(max_depth=2)
    out = await service.spawn_fan(
        _ctx(depth=1, tree=tree),
        {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]},
    )
    assert "refused" not in out.lower()
    assert _FakeLoop.calls and _FakeLoop.calls[-1]["depth"] == 2


# --- deepest-research R2: the one-shot decomposition sub-fan ----------------
# A task agent (depth 1, run seeded max_depth=2) may split its sub-question into ONE
# bounded fan of depth-2 sub agents via decompose_research. The amplification controls
# (depth guard, one-shot, per-parent cap K) and the transitivity (a sub agent can never
# hold decompose_research) live in SpawnService.decompose_fan.


async def test_decompose_refused_at_depth_0(service: SpawnService) -> None:
    """The orchestrator (depth 0) plans its own fan directly; decompose is task-agent-only."""
    out = await service.decompose_fan(
        _ctx(depth=0, tree=TreeState(max_depth=2)),
        {"subtopics": [{"title": "A", "brief": "research A"}]},
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


async def test_decompose_refused_for_a_leaf_sub_agent(service: SpawnService) -> None:
    """A depth-2 sub agent is a leaf under max_depth=2 — it cannot decompose further."""
    out = await service.decompose_fan(
        _ctx(depth=2, tree=TreeState(max_depth=2)),
        {"subtopics": [{"title": "A", "brief": "research A"}]},
    )
    assert "refused" in out.lower() and "leaf" in out.lower()
    assert not _FakeLoop.calls


async def test_decompose_spawns_research_sub_agents_one_tier_down(service: SpawnService) -> None:
    """A depth-1 task agent under max_depth=2 spawns its sub-fan at depth 2, and each sub
    agent is a plain `research` child — its clamped tools EXCLUDE decompose_research, so
    the recursion is exactly one tier (transitivity: sub ⊆ task, minus decompose)."""
    tree = TreeState(max_depth=2)
    out = await service.decompose_fan(
        _ctx(depth=1, tree=tree),
        {
            "subtopics": [
                {"title": "A", "brief": "research topic A"},
                {"title": "B", "brief": "research topic B"},
            ]
        },
    )
    assert "refused" not in out.lower()
    assert len(_FakeLoop.calls) == 2
    assert all(c["depth"] == 2 for c in _FakeLoop.calls)
    assert all("decompose_research" not in c["tools_allow"] for c in _FakeLoop.calls)
    assert tree.has_decomposed("parent-sess")  # one-shot recorded


async def test_decompose_is_one_shot(service: SpawnService) -> None:
    """A task agent decomposes at most once — the second call is refused so it cannot read
    its first sub-fan's fetched content and spawn a second fan embedding it (lateral exfil)."""
    tree = TreeState(max_depth=2)
    ctx = _ctx(depth=1, tree=tree)
    first = await service.decompose_fan(ctx, {"subtopics": [{"brief": "topic A"}]})
    assert "refused" not in first.lower()
    _FakeLoop.calls = []
    second = await service.decompose_fan(ctx, {"subtopics": [{"brief": "topic B"}]})
    assert "refused" in second.lower() and "once" in second.lower()
    assert not _FakeLoop.calls  # no second fan launched


async def test_decompose_caps_the_subfan_at_K(service: SpawnService) -> None:
    """At most MAX_SUBFAN_PER_TASK_AGENT sub agents per decomposition — the per-parent
    amplification bound on a laundered task-agent brief."""
    over = [{"brief": f"topic {i}"} for i in range(MAX_SUBFAN_PER_TASK_AGENT + 1)]
    out = await service.decompose_fan(
        _ctx(depth=1, tree=TreeState(max_depth=2)), {"subtopics": over}
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls
