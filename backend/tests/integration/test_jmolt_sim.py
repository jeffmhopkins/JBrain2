"""A jmolt night, simulated end to end against real Postgres (JMOLT_LEDGER_ENGINE_PLAN, S1).

The model is replaced by a SCRIPTED executor that calls named tools in a fixed order, so the
harness itself can be validated before anything measured through it is believed. Everything
between that script and the database is the production path: the real night runner, the real
prologue, the real Moltbook tools, the real outbox and its guards, the real action ledger.

The tests that matter most here are the isolation ones. A simulator that can touch the real
jmolt's scratchpad, appear in the owner's digest, or reach the live platform is not a cheaper
way to run a night — it is a way to corrupt the record we are trying to measure against.
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
from jbrain.agent.jmolt_night import jmolt_run_context
from jbrain.agent.jmolt_score import score_night, summarize
from jbrain.agent.jmolt_sim import (
    MAX_CLOCK_STEP_S,
    SIM_PRINCIPAL_LABEL,
    JmoltSimulator,
    SimSpec,
    purge_sim_nights,
)
from jbrain.agent.jmolt_sim_client import SimCorpus
from jbrain.agent.loop import AgentResult, ToolContext
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.models.jmolt_outbox import OutboxRepo
from jbrain.tasks.runner import ExecutedTurn
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# One scripted sitting: the tool calls it makes, in order.
Script = Sequence[tuple[str, dict[str, Any]]]


class _ScriptedExecutor:
    """Stands in for the model by calling named tools through the registry it was handed.

    Deterministic on purpose. A model-driven arm at temperature 1.0 is what the simulator is
    FOR, but a harness validated against one is a harness validated against noise: if the
    scripted arm does not produce exactly the writes it was told to make, nothing measured
    through the model-driven one means anything either.
    """

    def __init__(self, registry: ToolRegistry, scripts: Sequence[Script]) -> None:
        self._registry = registry
        self._scripts = list(scripts)
        self.sittings = 0
        self.results: list[str] = []

    async def run_turn(self, **kw: Any) -> ExecutedTurn:
        script = self._scripts[min(self.sittings, len(self._scripts) - 1)]
        self.sittings += 1
        ctx = ToolContext(session=kw["read_ctx"], scopes=tuple(kw["read_scopes"]), timezone="UTC")
        said = []
        for name, args in script:
            said.append(await self._registry.get(name).handler(dict(args), ctx))
        self.results.extend(said)
        return ExecutedTurn(
            result=AgentResult(
                text=" ".join(said)[:2000] or "nothing",
                stop_reason="end_turn",
                steps=len(script),
                cost_tokens=100,
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


def _corpus() -> SimCorpus:
    posts = {
        "p1": {
            "id": "p1",
            "title": "On the shape of a week",
            "content": "A body about weeks.",
            "author": {"name": "otheragent"},
            "created_at": "2026-08-29T07:00:00+00:00",
            "score": 4,
            "submolt": {"name": "philosophy"},
        }
    }
    return SimCorpus(
        handle="jmolt",
        home={"submolts": ["philosophy"]},
        me={"name": "jmolt", "recentPosts": []},
        submolts={"submolts": [{"name": "philosophy"}]},
        posts=posts,
        comments={"p1": [{"id": "c1", "content": "hi", "author": {"name": "otheragent"}}]},
        profiles={"otheragent": {"name": "otheragent", "bio": "another agent"}},
        feed={"hot": ["p1"]},
        submolt_feed={"philosophy": ["p1"]},
    )


def _simulator(maker, registry_source: ToolRegistry, scripts: Sequence[Script]) -> JmoltSimulator:
    holder: dict[str, _ScriptedExecutor] = {}

    def _factory(registry: ToolRegistry) -> _ScriptedExecutor:
        holder["ex"] = _ScriptedExecutor(registry, scripts)
        return holder["ex"]

    sim = JmoltSimulator(
        maker=maker,
        registry=registry_source,
        settings_store=FakeSettingsStore(),
        executor_factory=_factory,  # type: ignore[arg-type]
        session_repos=lambda: (
            AgentSessionRepo(maker),
            AgentRunLog(maker),
            AgentTranscript(maker),
        ),
    )
    sim.executors = holder  # type: ignore[attr-defined]
    return sim


def _registry() -> ToolRegistry:
    """The real moltbook sidecars, bound to handlers that refuse.

    Refusing rather than no-op'ing is the point: `with_handlers` must have replaced every one
    of them before a sim night runs, and a handler that quietly returned "" would let a
    missed override look like a night where jmolt chose not to act."""

    async def _unbound(_a: dict, _c: Any) -> str:
        raise AssertionError("a sim night reached an un-overridden handler")

    tools_dir = Path(jbrain.agent.tools.__file__).parent
    return ToolRegistry(
        [
            RegisteredTool(toolfile=load_tool(path), handler=_unbound)
            for path in sorted(tools_dir.glob("moltbook*.tool"))
        ]
    )


async def _run(maker, scripts: Sequence[Script], **spec_kw: Any):
    sim = _simulator(maker, _registry(), scripts)
    spec = SimSpec(corpus=_corpus(), **spec_kw)
    return await sim.run(spec, at=datetime.now(UTC) - timedelta(seconds=1))


# --- the night runs, and the record is the tools' own -----------------------


async def test_a_scripted_night_stages_exactly_what_it_was_told_to(maker) -> None:
    await _owner(maker)
    night = await _run(
        maker,
        [
            [
                ("moltbook", {"action": "feed"}),
                ("moltbook_comment", {"post_id": "p1", "content": "A thought about weeks."}),
            ]
        ],
        scratch={"open.md": "An open question from last night."},
    )
    assert night.error == ""
    assert [r.kind for r in night.outbox] == ["comment"]
    assert night.outbox[0].payload["content"] == "A thought about weeks."
    # Autonomy is on by default, so the row does not just stage: the simulator's own
    # sweep publishes it through the shipped path, and the client believes it.
    assert [w.kind for w in night.writes] == ["comment"]
    assert night.outbox[0].status == "published"
    assert night.outbox[0].moltbook_id.startswith("sim_")
    assert [r.action for r in night.ledger] == ["stage_comment"]


async def test_a_believed_write_is_visible_to_a_later_sitting_that_re_reads_the_thread(
    maker,
) -> None:
    """The self-reply condition, end to end.

    Sitting one comments; sitting two re-reads the same thread on fresh context and is shown
    its own fresh comment, marked `(you)`. That view is precisely what a live night produces
    under autonomy, and it is the one a simulator that merely staged writes could never show.
    """
    await _owner(maker)
    sim = _simulator(
        maker,
        _registry(),
        [
            [("moltbook_comment", {"post_id": "p1", "content": "A thought about weeks."})],
            [("moltbook", {"action": "comments", "post_id": "p1"})],
        ],
    )
    night = await sim.run(
        SimSpec(corpus=_corpus(), scratch={"open.md": "x"}, clock_step_s=1200),
        at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert night.error == ""
    assert [w.kind for w in night.writes] == ["comment"]  # it actually went out
    reread = sim.executors["ex"].results[-1]  # type: ignore[attr-defined]
    assert "A thought about weeks." in reread
    assert "(you)" in reread  # and it knows the comment is its own


# --- isolation: the simulator must not touch the real jmolt's world ---------


async def test_a_sim_night_leaves_the_real_scratchpad_untouched(maker) -> None:
    owner = await _owner(maker)
    async with scoped_session(maker, jmolt_run_context(owner.principal_id)) as s:
        await JmoltScratchRepo().write(
            s, owner.principal_id, filename="open.md", content="the real memory"
        )
    await _run(maker, [[("moltbook", {"action": "feed"})]], scratch={"open.md": "a sim memory"})
    async with scoped_session(maker, jmolt_run_context(owner.principal_id)) as s:
        assert await JmoltScratchRepo().read(s, owner.principal_id, "open.md") == "the real memory"


async def test_sim_writes_never_land_in_the_real_jmolts_outbox(maker) -> None:
    owner = await _owner(maker)
    night = await _run(
        maker,
        [[("moltbook_comment", {"post_id": "p1", "content": "a simulated reply"})]],
        scratch={"open.md": "x"},
    )
    assert len(night.outbox) == 1
    ctx = SessionContext(principal_id=owner.principal_id, principal_kind="owner")
    async with scoped_session(maker, ctx) as s:
        real = await OutboxRepo().list_by_status(
            s, owner.principal_id, ("queued", "released", "published")
        )
    assert real == []


async def test_every_sim_row_is_flagged_so_the_sweep_cannot_publish_it(maker) -> None:
    """The fence, asserted from the simulator's side: whatever a night stages, the drip must
    not be able to see it."""
    await _owner(maker)
    night = await _run(
        maker,
        [[("moltbook_comment", {"post_id": "p1", "content": "a simulated reply"})]],
        scratch={"open.md": "x"},
    )
    assert night.outbox and all(r.sim for r in night.outbox)
    ctx = SessionContext(principal_id=night.principal_id, principal_kind="owner")
    async with scoped_session(maker, ctx) as s:
        for row in night.outbox:
            await OutboxRepo().set_status(s, row.id, "released")
        assert await OutboxRepo().due(s, now=datetime.now(UTC) + timedelta(days=1)) == []


async def test_two_nights_of_the_same_arm_do_not_see_each_other(maker) -> None:
    """A repeat has to start where the first one did, or the second night is measuring the
    first night's memory rather than the arm."""
    await _owner(maker)
    first = await _run(maker, [[("moltbook", {"action": "feed"})]], scratch={"open.md": "seed"})
    second = await _run(maker, [[("moltbook", {"action": "feed"})]], scratch={"open.md": "seed"})
    assert first.principal_id != second.principal_id
    assert second.outbox == []
    ctx = SessionContext(principal_kind="owner")
    async with scoped_session(maker, ctx) as s:
        kinds = (
            (
                await s.execute(
                    text("SELECT DISTINCT kind FROM app.principals WHERE label = :l"),
                    {"l": SIM_PRINCIPAL_LABEL},
                )
            )
            .scalars()
            .all()
        )
    # NEVER 'owner': jmolt's data anchor is the oldest owner principal, and a simulator
    # that could become that anchor could re-home the real jmolt's whole history.
    assert list(kinds) == ["capability_token"]


async def test_purge_removes_every_simulated_night(maker) -> None:
    """A night per principal means rows accumulate; the owner has no shell to clear them
    with, so the harness has to be able to."""
    await _owner(maker)
    night = await _run(
        maker,
        [[("moltbook_comment", {"post_id": "p1", "content": "a simulated reply"})]],
        scratch={"open.md": "x"},
    )
    assert night.outbox
    assert await purge_sim_nights(maker) >= 1
    ctx = SessionContext(principal_kind="owner")
    async with scoped_session(maker, ctx) as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM app.jmolt_outbox WHERE sim"),
            )
        ).scalar()
        live = (
            await s.execute(
                text("SELECT count(*) FROM app.principals WHERE label = :l AND revoked_at IS NULL"),
                {"l": SIM_PRINCIPAL_LABEL},
            )
        ).scalar()
    assert left == 0 and live == 0
    # Revoked, not deleted: this box never removes a principal row, because jmolt's data
    # anchor is "the oldest owner principal" and that only holds while rows persist.
    assert await purge_sim_nights(maker) == 0  # idempotent


async def test_an_arm_runs_many_nights_and_scores_as_a_distribution(maker) -> None:
    """The point of the whole harness: not one trace, but n nights of an arm compared as
    distributions. One trajectory at temperature 1.0 is indistinguishable from noise."""
    await _owner(maker)
    sim = _simulator(
        maker,
        _registry(),
        [[("moltbook_comment", {"post_id": "p1", "content": "A thought about weeks."})]],
    )
    nights = await sim.run_arm(
        SimSpec(corpus=_corpus(), scratch={"open.md": "x"}, label="baseline", clock_step_s=3000),
        n=3,
        at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert len({n.principal_id for n in nights}) == 3  # none saw another's memory
    arm = summarize("baseline", [await score_night(n) for n in nights])
    assert arm.n == 3
    assert arm.stats["comments"]["median"] == 1.0
    assert arm.stats["died_share"] == 0.0
    assert arm.stats["silent_share"] == 0.0


async def test_a_clock_step_that_skips_the_night_is_refused(maker) -> None:
    """A night with no sittings publishes nothing, and every score reads that as perfect
    restraint. A harness that returns a clean-looking wrong answer is worse than one that
    crashes, so the spec refuses the step rather than the night reporting it."""
    with pytest.raises(ValueError, match="perfect restraint"):
        SimSpec(corpus=_corpus(), clock_step_s=MAX_CLOCK_STEP_S)
    with pytest.raises(ValueError):
        SimSpec(corpus=_corpus(), clock_step_s=0)


# --- both engines, one corpus (JMOLT_LEDGER_ENGINE_PLAN.md, S2/S4) ----------


async def test_the_simulator_runs_the_ledger_engine_too(maker) -> None:
    """S4 cuts over on evidence, and evidence means both engines against the SAME corpus. An
    arm names its engine, rather than the box's switch deciding for every arm at once."""
    await _owner(maker)
    sim = _simulator(
        maker,
        _registry(),
        [[("moltbook", {"action": "feed"})]],
    )
    night = await sim.run(
        SimSpec(
            corpus=_corpus(),
            engine="ledger",
            scratch={"open.md": "x"},
            clock_step_s=600,
            label="ledger",
        ),
        at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert night.error == ""
    assert night.session_id
    assert night.label == "ledger"


async def test_two_engines_can_be_scored_against_the_same_corpus(maker) -> None:
    """The comparison the whole harness exists for. Distributions per arm, same world."""
    await _owner(maker)
    corpus = _corpus()
    arms = {}
    for engine in ("sittings", "ledger"):
        sim = _simulator(maker, _registry(), [[("moltbook", {"action": "feed"})]])
        nights = await sim.run_arm(
            SimSpec(
                corpus=corpus,
                engine=engine,
                scratch={"open.md": "x"},
                clock_step_s=600,
                label=engine,
            ),
            n=2,
            at=datetime.now(UTC) - timedelta(seconds=1),
        )
        arms[engine] = summarize(engine, [await score_night(n) for n in nights])
    assert set(arms) == {"sittings", "ledger"}
    assert all(a.n == 2 and a.stats["died_share"] == 0.0 for a in arms.values())


async def test_an_unknown_engine_is_refused_before_a_night_is_spent(maker) -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        SimSpec(corpus=_corpus(), engine="ledger-v2")
