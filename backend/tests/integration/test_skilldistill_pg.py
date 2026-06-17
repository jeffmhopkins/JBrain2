"""The `skill_distill` action (Loop 2, Wave 2) end-to-end against real Postgres: a successful agent
run is distilled into a SHADOW skill + an owner `skill-promotion` proposal (never auto-active);
enacting the proposal flips it to active; the budget kill-switch refuses; the high-water mark stops
a run being distilled twice. The LLM router is faked (no model); embeddings are deterministic."""

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.connectortools import build_leaf_executor
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.skilldistill import SkillDistillAction
from jbrain.agent.skills import SkillsRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.settings_store import (
    SELF_IMPROVEMENT_KILL_SWITCH_KEY,
    SqlSettingsStore,
)
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


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


async def _seed_run(maker: async_sessionmaker, owner: SessionContext) -> str:
    """A successful agent run with two tool steps + an assistant turn — a distillation candidate."""
    info = await AgentSessionRepo(maker).create(owner, domain_scopes=["general"], title="t")
    log = AgentRunLog(maker)
    run_id = await log.start(owner, session_id=info.id, prompt_version="v1")
    async with scoped_session(maker, owner) as s:
        for i, name in enumerate(["search", "read_note"]):
            await s.execute(
                text(
                    "INSERT INTO app.run_steps (id, run_id, idx, kind, name, ok, cost_tokens)"
                    " VALUES (gen_random_uuid(), :r, :i, 'tool', :n, true, 0)"
                ),
                {"r": run_id, "i": i, "n": name},
            )
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, run_id, role, content, tools)"
                " VALUES (gen_random_uuid(), :s, :r, 'assistant', :c, '[]'::jsonb)"
            ),
            {"s": info.id, "r": run_id, "c": "I searched and cited the note."},
        )
    await log.finish(
        owner, run_id, status="done", stop_reason="end_turn", step_count=3, cost_tokens=10
    )
    return run_id


def _action(maker: async_sessionmaker, payload: dict) -> SkillDistillAction:
    fake = FakeLlmClient(responses=[json.dumps(payload)])
    router = LlmRouter({"xai": fake}, {}, tiers={"high": ("xai", "m")})
    return SkillDistillAction(
        maker,
        router=router,
        embedder=FakeEmbed(),  # type: ignore[arg-type]
        embedding_model="fake",
        settings=SqlSettingsStore(maker),
        skills=SkillsRepo(maker),
        proposals=ProposalRepo(maker),
    )


_GOOD = {
    "name": "Cite a fact",
    "description": "find and cite a note",
    "body": "1. search for {topic} 2. read_note the hit 3. cite it",
    "reusable": True,
}


async def _skills(maker: async_sessionmaker, owner: SessionContext) -> list[tuple[str, str]]:
    async with scoped_session(maker, owner) as s:
        rows = (await s.execute(text("SELECT id::text, status FROM app.skills"))).all()
    return [(r.id, r.status) for r in rows]


async def test_distill_creates_shadow_and_owner_proposal_then_enact_activates(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    await _action(maker, _GOOD).run({})

    skills = await _skills(maker, owner)
    assert len(skills) == 1 and skills[0][1] == "shadow"  # distilled, NOT auto-active
    proposals = ProposalRepo(maker)
    open_props = await proposals.list_open(owner)
    assert [p.kind for p in open_props] == ["skill-promotion"]

    # The owner approves + enacts → the shadow skill flips to active (the promotion gate). Only the
    # skill executor is exercised here (the leaf op is skill_promote), so the others are unused.
    prop_id = open_props[0].id
    _proposal, nodes = await proposals.load(owner, prop_id)
    await proposals.decide(owner, nodes[0].id, approve=True)
    none: Any = cast(Any, None)
    executor = build_leaf_executor(none, none, none, none, SkillsRepo(maker))
    await proposals.enact(owner, prop_id, executor)
    assert (await _skills(maker, owner))[0][1] == "active"


async def test_distill_refused_when_kill_switch_on(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    await SqlSettingsStore(maker).upsert(owner, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    from jbrain.queue import PermanentJobError

    with pytest.raises(PermanentJobError):
        await _action(maker, _GOOD).run({})
    assert await _skills(maker, owner) == []  # nothing distilled behind the gate


async def test_high_water_mark_prevents_redistillation(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    await _action(maker, _GOOD).run({})
    assert len(await _skills(maker, owner)) == 1
    # A second sweep with no new runs distills nothing more (the run is past the high-water mark).
    await _action(maker, _GOOD).run({})
    assert len(await _skills(maker, owner)) == 1
