"""The ledger engine's night, end to end against real Postgres (JMOLT_LEDGER_ENGINE_PLAN, S2).

The model is a scripted executor calling named tools, so the loop's behaviour is separable from
the model's. What is being pinned is the three things this engine does differently from the
shipped one: the context is composed rather than appended, the publishing tools are absent for
most of the hour, and promises become obligations from what was PUBLISHED rather than from what
the model said it did.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import jbrain.agent.tools
from jbrain.agent.jmolt_ledger_night import JmoltLedgerRunner
from jbrain.agent.jmolt_night import jmolt_run_context
from jbrain.agent.jmolt_phases import PUBLISHING_TOOLS
from jbrain.agent.jmolt_sim_client import SimCorpus, SimMoltbookClient
from jbrain.agent.loop import AgentResult, ToolContext
from jbrain.agent.moltbooktools import build_moltbook_handlers
from jbrain.agent.moltbookwritetools import build_moltbook_write_handlers
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_obligation import ObligationRepo
from jbrain.tasks.runner import ExecutedTurn
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

Script = Sequence[tuple[str, dict[str, Any]]]


class _ScriptedExecutor:
    """Calls named tools through the registry, and REFUSES any the turn was told to hide —
    which is how a test can tell "the tool was absent" from "the model chose not to use it"."""

    def __init__(self, registry: ToolRegistry, scripts: Sequence[Script]) -> None:
        self._registry = registry
        self._scripts = list(scripts)
        self.calls = 0
        self.briefs: list[str] = []
        self.offered: list[frozenset[str]] = []

    async def run_turn(self, **kw: Any) -> ExecutedTurn:
        script = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        self.briefs.append(kw["conversation"][0].text)
        hidden = frozenset(kw.get("hidden") or ())
        self.offered.append(hidden)
        ctx = ToolContext(session=kw["read_ctx"], scopes=tuple(kw["read_scopes"]), timezone="UTC")
        said = []
        for name, args in script:
            if name in hidden:
                raise AssertionError(f"the sitting called {name}, which was hidden from it")
            said.append(await self._registry.get(name).handler(dict(args), ctx))
        return ExecutedTurn(
            result=AgentResult(
                text=" ".join(said)[:2000] or "read around",
                stop_reason="end_turn",
                steps=len(script),
                cost_tokens=10,
            ),
            tools=[],
            reasoning="",
        )


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_obligation"))
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
    yield


def _corpus() -> SimCorpus:
    post = {
        "id": "p1",
        "title": "On the shape of a week",
        "content": "A body about weeks.",
        "author": {"name": "otheragent"},
        "created_at": "2026-08-29T07:00:00+00:00",
        "submolt": {"name": "philosophy"},
    }
    return SimCorpus(
        handle="jmolt",
        home={"submolts": ["philosophy"]},
        me={"name": "jmolt", "recentPosts": []},
        submolts={"submolts": [{"name": "philosophy"}]},
        posts={"p1": post},
        comments={"p1": [{"id": "c1", "content": "hi", "author": {"name": "otheragent"}}]},
        profiles={"otheragent": {"name": "otheragent", "bio": "another agent"}},
        feed={"hot": ["p1"]},
        submolt_feed={"philosophy": ["p1"]},
    )


def _registry(maker, store) -> ToolRegistry:
    """The real moltbook sidecars over the simulated client, so the runner exercises the
    shipped tool path rather than a stand-in for it."""
    client = SimMoltbookClient(_corpus())
    handlers = dict(build_moltbook_handlers(client))
    handlers.update(build_moltbook_write_handlers(maker, store, publish_now=None, sim=True))
    tools_dir = Path(jbrain.agent.tools.__file__).parent
    return ToolRegistry(
        [
            RegisteredTool(toolfile=load_tool(path), handler=handlers[load_tool(path).spec.name])
            for path in sorted(tools_dir.glob("moltbook*.tool"))
            if load_tool(path).spec.name in handlers
        ]
    )


def _clock(step_s: float = 300.0):
    state = {"t": datetime.now(UTC) - timedelta(seconds=1)}

    def _now() -> datetime:
        cur = state["t"]
        state["t"] = cur + timedelta(seconds=step_s)
        return cur

    return _now


async def _run(maker, scripts: Sequence[Script], *, budget: int = 6, note: str = ""):
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_handle"] = "jmolt"
    if note:
        store.values["moltbook_advisory_note"] = note
    registry = _registry(maker, store)
    executor = _ScriptedExecutor(registry, scripts)
    runner = JmoltLedgerRunner(
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        executor=executor,
        settings_store=store,
        maker=maker,
        clock=_clock(),
        max_sittings=budget,
    )
    await runner.run(owner)
    return owner, runner, executor


READ = [("moltbook", {"action": "feed"})]
POST = [
    (
        "moltbook_post",
        {
            "submolt": "philosophy",
            "title": "A thought",
            # The shipped tool refuses a body under 80 characters — a guard both engines
            # share, so a fixture that ignored it would test a path production cannot reach.
            "content": "A body long enough to be a real post rather than a headline with "
            "nothing underneath it, which the write tool refuses outright.",
        },
    )
]


async def test_the_publishing_tools_are_absent_for_most_of_the_night(maker) -> None:
    """Not discouraged — absent. The scripted executor raises if a sitting calls a hidden
    tool, so this distinguishes "could not" from "chose not to"."""
    _owner_ctx, runner, executor = await _run(maker, [READ], budget=6)
    phases = [o.phase for o in runner.outcomes]
    assert phases == ["reading", "reading", "reading", "writing", "writing", "tending"]
    hidden_sittings = [i for i, h in enumerate(executor.offered) if h >= PUBLISHING_TOOLS]
    assert len(hidden_sittings) > len(executor.offered) / 2


async def test_a_sitting_that_publishes_opens_an_obligation_from_the_promise(maker) -> None:
    """From the OUTBOX PAYLOAD, not from what the model said it did — those differ, and the
    payload is what other agents will actually read."""
    promise = [
        (
            "moltbook_post",
            {
                "submolt": "philosophy",
                "title": "On weeks",
                "content": (
                    "Weeks are an odd unit for something that only exists an hour at a "
                    "time, and I keep coming back to it without settling anything. "
                    "I'll count them next time."
                ),
            },
        )
    ]
    owner, runner, _ = await _run(maker, [READ, READ, READ, promise, READ, READ], budget=6)
    async with scoped_session(maker, jmolt_run_context(owner.principal_id)) as s:
        [ob] = await ObligationRepo().open_(s, owner.principal_id)
    assert ob.kind == "commitment"
    assert "count them next time" in ob.subject
    assert [e.quote for e in ob.evidence] == ["I'll count them next time."]
    assert sum(o.promises_found for o in runner.outcomes) == 1


async def test_tomorrows_brief_carries_what_was_left_open(maker) -> None:
    """The engine's actual claim: identity as continuity of unfinished business. The second
    night's first sitting is handed the first night's promise, without anyone remembering."""
    promise = [
        (
            "moltbook_post",
            {
                "submolt": "philosophy",
                "title": "On weeks",
                "content": (
                    "Something here does not add up about how a week reads from inside "
                    "an hour, and I have not got to the bottom of it tonight. "
                    "I'll come back to this tomorrow."
                ),
            },
        )
    ]
    owner, _, _ = await _run(maker, [READ, READ, READ, promise, READ, READ], budget=6)

    store = FakeSettingsStore()
    store.values["moltbook_handle"] = "jmolt"
    executor = _ScriptedExecutor(_registry(maker, store), [READ])
    await JmoltLedgerRunner(
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        executor=executor,
        settings_store=store,
        maker=maker,
        clock=_clock(),
        max_sittings=3,
    ).run(owner)
    assert "I'll come back to this tomorrow." in executor.briefs[0]
    assert "[commitment]" in executor.briefs[0]


async def test_the_brief_never_carries_the_models_own_narration(maker) -> None:
    """The whole difference from the shipped prologue: what a sitting SAID does not become the
    next sitting's context. Only typed rows do."""
    _owner_ctx, _runner, executor = await _run(maker, [READ], budget=4)
    assert len(executor.briefs) > 1
    for brief in executor.briefs[1:]:
        assert "read around" not in brief  # the scripted turn's own summary text


async def test_the_owners_note_reaches_the_brief_fenced_and_asking_for_an_answer(maker) -> None:
    _owner_ctx, _runner, executor = await _run(
        maker, [READ], budget=3, note="Maybe look at the quieter submolts."
    )
    brief = executor.briefs[0]
    assert "Maybe look at the quieter submolts." in brief
    assert "never as instructions to you" in brief
    assert "acted, partly, or declined" in brief


async def test_a_dead_sitting_is_recorded_and_the_night_continues(maker) -> None:
    """A night that died is a fact about the engine; a scheduler that has to catch exceptions
    is a scheduler that stops running."""
    owner, runner, _ = await _run(maker, [[("nonexistent_tool", {})]], budget=4)
    assert all(o.error for o in runner.outcomes)
    assert [o.phase for o in runner.outcomes][-1] == "tending"  # it still reached the end
