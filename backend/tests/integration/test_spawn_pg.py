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
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.agent.tree import TreeState
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.types import LlmTurn, LlmUsage, TextChunk
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
    streaming/non-streaming turn that answers immediately (no tools, no LLM). A child
    streams (on_text set), so the loop calls `converse_stream`."""

    async def effective_reasoning_effort(self, task: str, strength: str | None = None) -> str:
        return "high"

    async def effective_spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        return ("xai", "grok-4.3")  # non-local → the fan stays parallel

    async def context_window(self, task: str) -> int:
        return 131_072  # the child meter's denominator

    async def converse(
        self, task: str, *, system, messages, tools, max_tokens, strength=None, effort_override=None
    ):  # noqa: ANN001, ANN003, E501
        return LlmTurn(
            text="child summary", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(5, 5)
        )

    async def converse_stream(
        self, task: str, *, system, messages, tools, max_tokens, strength=None, effort_override=None
    ):  # noqa: ANN001, ANN003, E501
        yield TextChunk(text="child summary")
        yield LlmTurn(
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
    transcript = AgentTranscript(maker)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=ToolRegistry([]),
        sessions=sessions,
        runlog=runlog,
        transcript=transcript,
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

    # …but the child's brief→answer IS in its own transcript, so opening the
    # sub-agent in the sessions rail replays its work instead of an empty chat.
    turns = await transcript.load(owner, child.id)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].content == "what is HNSW?"
    assert turns[1].content == "child summary"


async def test_only_the_root_may_spawn_children_are_leaves(maker: async_sessionmaker) -> None:
    """Nesting removed: only jerv (depth 0) may spawn; a depth-1 child is refused
    outright — the tree is exactly two levels, enforced with no model cooperation."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    runlog = AgentRunLog(maker)
    transcript = AgentTranscript(maker)
    svc = SpawnService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        registry=ToolRegistry([]),
        sessions=sessions,
        runlog=runlog,
        transcript=transcript,
    )
    parent = await sessions.create(owner, domain_scopes=[], title="root", agent="jerv")
    tree = TreeState()

    # A depth-0 root fan is admitted (jerv spawning its leaves).
    ctx0 = ToolContext(
        session=owner,
        scopes=(),
        agent_session_id=parent.id,
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=tree,
    )
    ok = await svc.spawn_fan(
        ctx0, {"tasks": [{"persona": "research", "brief": "x", "label": "leaf"}]}
    )
    assert "child summary" in ok

    # A depth-1 child cannot spawn its own children — refused outright.
    ctx1 = ToolContext(
        session=owner,
        scopes=(),
        agent_session_id=parent.id,
        depth=1,
        agent_tools=JERV_TOOLS,
        tree=tree,
    )
    refused = await svc.spawn_fan(
        ctx1, {"tasks": [{"persona": "research", "brief": "x", "label": "L"}]}
    )
    assert "refused" in refused.lower()
