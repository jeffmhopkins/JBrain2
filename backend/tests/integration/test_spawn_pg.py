"""The sub-agent spawn service against real Postgres (docs/SUBAGENT_SPAWNING_PLAN.md,
Wave S1.3): a fan mints children with the real lineage + sandbox flag, child runs are
kind='subagent' with parent_run_id, and — the load-bearing sandbox invariant — a
child turn writes NO episodic-memory row. Uses a minimal fake router (the loop needs
only `converse` + `effective_reasoning_effort`) and an empty registry, so a child runs
a real AgentLoop turn end-to-end without an LLM or any tools.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.loop import ToolContext
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.spawn import SpawnService
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.tree import TreeState
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.types import LlmTurn, LlmUsage
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _FakeRouter:
    """The minimal router surface the loop + spawn service use: an effort read and a
    `converse` that answers immediately (no tools, no LLM)."""

    async def effective_reasoning_effort(self, task: str, strength: str | None = None) -> str:
        return "high"

    async def converse(
        self, task: str, *, system, messages, tools, max_tokens, strength=None, effort_override=None
    ):  # noqa: ANN001, ANN003, E501
        return LlmTurn(
            text="child summary", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(5, 5)
        )


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_fan_persists_sandboxed_lineage_and_writes_no_episode(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    runlog = AgentRunLog(maker)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=ToolRegistry([]),
        sessions=sessions,
        runlog=runlog,
    )

    parent = await sessions.create(owner, domain_scopes=[], title="root", agent="jerv")
    parent_run = await runlog.start(owner, session_id=parent.id, prompt_version="jerv-v")
    ctx = ToolContext(
        session=owner,
        scopes=(),
        agent_session_id=parent.id,
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState(),
        run_id=parent_run,
    )

    out = await svc.spawn_fan(
        ctx, {"tasks": [{"persona": "research", "brief": "what is HNSW?", "label": "HNSW"}]}
    )
    assert "child summary" in out

    # The child session persisted with the sub-agent lineage + sandbox flag.
    children = [s for s in await sessions.list(owner) if s.parent_session_id == parent.id]
    assert len(children) == 1
    child = children[0]
    assert child.agent == "research"
    assert child.depth == 1
    assert child.no_memory is True

    async with scoped_session(maker, owner) as session:
        kind = (
            await session.execute(
                text("SELECT kind FROM app.runs WHERE session_id = :sid AND parent_run_id = :pr"),
                {"sid": child.id, "pr": parent_run},
            )
        ).scalar()
        episodes = (await session.execute(text("SELECT count(*) FROM app.agent_episodes"))).scalar()
    assert kind == "subagent"
    # The load-bearing sandbox invariant: a child turn appends no episodic memory.
    assert episodes == 0


async def test_depth1_child_may_spawn_but_a_depth2_leaf_cannot(maker: async_sessionmaker) -> None:
    """A depth-1 child is below the cap and may fan a grandchild (template-bound); a
    depth-2 leaf is refused — the two-sub-agent-layer cap, with no model cooperation."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    runlog = AgentRunLog(maker)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=ToolRegistry([]),
        sessions=sessions,
        runlog=runlog,
    )
    parent = await sessions.create(owner, domain_scopes=[], title="root", agent="jerv")
    tree = TreeState()

    # A depth-1 spawner with a template-bound brief is admitted.
    ctx1 = ToolContext(
        session=owner,
        scopes=(),
        agent_session_id=parent.id,
        depth=1,
        agent_tools=JERV_TOOLS,
        tree=tree,
    )
    ok = await svc.spawn_fan(
        ctx1,
        {
            "tasks": [
                {
                    "persona": "research",
                    "label": "grandchild",
                    "brief": {
                        "template_id": "research",
                        "params": {"question": "q", "context": "c", "deliverable": "d"},
                    },
                }
            ]
        },
    )
    assert "child summary" in ok

    # A depth-2 leaf is refused outright.
    ctx2 = ToolContext(
        session=owner,
        scopes=(),
        agent_session_id=parent.id,
        depth=2,
        agent_tools=JERV_TOOLS,
        tree=tree,
    )
    refused = await svc.spawn_fan(
        ctx2, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "refused" in refused.lower()
