"""The sub-agent spawn service (docs/SUBAGENT_SPAWNING_PLAN.md, Wave S1): the
structural caps and sandbox wiring, proven with NO model cooperation. Refusal paths
return before any DB/model touch; the success path uses fakes + a loop seam so the
service's wiring (clamp, depth, no-location, tree threading, lineage) is asserted
without a database or an LLM."""

import pytest

from jbrain.agent import spawn as spawn_mod
from jbrain.agent.agents import AGENTS, JERV_TOOLS
from jbrain.agent.loop import AgentResult, ToolContext
from jbrain.agent.spawn import SpawnService, effective_child_tools
from jbrain.agent.tree import MAX_CHILDREN_PER_PARENT, MAX_DEPTH, MAX_PARALLEL, TreeState
from jbrain.db.session import SessionContext

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


class _FakeLoop:
    """Records the kwargs each child run is launched with, so the service's wiring
    is observable without a real loop, DB, or model."""

    calls: list[dict] = []

    def __init__(self, *_a, **_k) -> None:
        pass

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


async def test_depth_cap_refuses_at_leaf(service: SpawnService) -> None:
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


async def test_depth1_free_text_brief_rejected(service: SpawnService) -> None:
    """At depth>=1 a brief MUST be template-bound — a free-text string is refused
    (the re-spawn laundering hop, decision #7)."""
    out = await service.spawn_fan(
        _ctx(depth=1), {"tasks": [{"persona": "research", "brief": "free text", "label": "L"}]}
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


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


async def test_budget_truncated_child_is_ok_but_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child cut off on budget has a real but partial answer: it counts as ok (it
    has content) yet the summary is tagged truncated so the parent doesn't treat it as
    complete."""

    class _TruncatedLoop(_FakeLoop):
        async def run(self, **kw):  # noqa: ANN003
            _FakeLoop.calls.append(kw)
            return AgentResult(
                text="partial findings", stop_reason="budget", steps=4, cost_tokens=9
            )

    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _TruncatedLoop)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
    )
    out = await svc.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "partial findings" in out
    assert "truncated" in out.lower()
    assert "[FAILED]" not in out  # it is ok (has content), just partial


async def test_step_limited_child_with_a_forced_answer_is_ok_but_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that hits max_steps but synthesized a final answer (force_final_answer)
    counts as ok — it has real content — and is tagged truncated, not [FAILED]. The
    child loop is launched with force_final_answer set."""

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
    out = await svc.spawn_fan(
        _ctx(), {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "best-effort findings" in out
    assert "truncated" in out.lower()
    assert "[FAILED]" not in out
    assert _FakeLoop.calls[0]["force_final_answer"] is True


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
    assert [e.type for e in captured] == [
        "subagent_spawned",
        "subagent_progress",
        "subagent_done",
    ]
    spawned, progress, done = captured
    assert (spawned.persona, spawned.label, spawned.depth) == ("research", "L", 1)
    assert spawned.child_id == "sess-1"
    assert progress.phase == "researching"
    assert done.ok is True and done.child_id == "sess-1"


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


async def test_fan_without_a_sink_does_not_emit(service: SpawnService) -> None:
    # A turn with no event sink (the non-streaming child path) simply skips emission —
    # no crash, so a grandchild fan degrades to summary-only (documented v1 limit).
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
