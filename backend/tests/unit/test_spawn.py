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
from jbrain.agent.tree import MAX_CHILDREN_PER_PARENT, MAX_DEPTH, TreeState
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
    async def effective_reasoning_effort(self, task):  # noqa: ANN001
        return "high"


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


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> SpawnService:
    _FakeLoop.calls = []
    monkeypatch.setattr(spawn_mod, "AgentLoop", _FakeLoop)
    return SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        sessions=_FakeSessions(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
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


async def test_depth1_free_text_brief_rejected(service: SpawnService) -> None:
    """At depth>=1 a brief MUST be template-bound — a free-text string is refused
    (the re-spawn laundering hop, decision #7)."""
    out = await service.spawn_fan(
        _ctx(depth=1), {"tasks": [{"persona": "research", "brief": "free text", "label": "L"}]}
    )
    assert "refused" in out.lower()
    assert not _FakeLoop.calls


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
