"""The `correction_mine` action (Loop 3b) end-to-end against real Postgres: an ended chat where the
owner corrected a fact is mined into an owner `correction` proposal (NEVER auto-applied); enacting
it creates a provenance-flagged agent note + enqueues ingestion (the shipped correction spine); the
kill-switch refuses; the high-water mark stops a run being mined twice. The LLM router is faked."""

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.connectortools import build_leaf_executor
from jbrain.agent.correctionmine import CorrectionMineAction
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.notes.repo import SqlNotesRepo
from jbrain.settings_store import SELF_IMPROVEMENT_KILL_SWITCH_KEY, SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class _Jobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind='owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _seed_run(maker: async_sessionmaker, owner: SessionContext) -> str:
    """An ended chat run whose session has two user turns (a correction back-and-forth)."""
    info = await AgentSessionRepo(maker).create(owner, domain_scopes=["general"], title="t")
    log = AgentRunLog(maker)
    run_id = await log.start(owner, session_id=info.id, prompt_version="v1")
    turns = [
        ("user", "my cardiologist is Dr. Lee"),
        ("assistant", "Got it — Dr. Patel is your cardiologist."),
        ("user", "no, it's Dr. Lee"),
    ]
    async with scoped_session(maker, owner) as s:
        for role, content in turns:
            await s.execute(
                text(
                    "INSERT INTO app.agent_turns (id, session_id, run_id, role, content, tools)"
                    " VALUES (gen_random_uuid(), :s, :r, :role, :c, '[]'::jsonb)"
                ),
                {"s": info.id, "r": run_id, "role": role, "c": content},
            )
    await log.finish(
        owner, run_id, status="done", stop_reason="end_turn", step_count=3, cost_tokens=10
    )
    return run_id


def _action(maker: async_sessionmaker, payload: dict) -> CorrectionMineAction:
    fake = FakeLlmClient(responses=[json.dumps(payload)])
    router = LlmRouter({"xai": fake}, {}, tiers={"high": ("xai", "m")})
    return CorrectionMineAction(
        maker,
        router=router,
        settings=SqlSettingsStore(maker),
        proposals=ProposalRepo(maker),
    )


_FOUND = {"found": True, "note": "My cardiologist is Dr. Lee."}


async def _note_count(maker: async_sessionmaker, owner: SessionContext) -> int:
    async with scoped_session(maker, owner) as s:
        return (await s.execute(text("SELECT count(*) FROM app.notes"))).scalar_one()


async def test_mines_owner_proposal_then_enact_creates_a_note(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    notes_before = await _note_count(maker, owner)

    await _action(maker, _FOUND).run({})

    proposals = ProposalRepo(maker)
    open_props = await proposals.list_open(owner)
    assert [p.kind for p in open_props] == ["correction"]
    assert await _note_count(maker, owner) == notes_before  # staged only, NOT auto-applied

    # Owner approves + enacts → the correction re-enters as an agent note + ingestion is enqueued.
    prop_id = open_props[0].id
    _proposal, nodes = await proposals.load(owner, prop_id)
    await proposals.decide(owner, nodes[0].id, approve=True)
    jobs = _Jobs()
    none: Any = cast(Any, None)
    executor = build_leaf_executor(SqlNotesRepo(maker), none, jobs, none, none)
    await proposals.enact(owner, prop_id, executor)
    assert await _note_count(maker, owner) == notes_before + 1
    assert jobs.enqueued == [("ingest_note", {"note_id": jobs.enqueued[0][1]["note_id"]})]


async def test_refused_when_kill_switch_on(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    await SqlSettingsStore(maker).upsert(owner, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    from jbrain.queue import PermanentJobError

    with pytest.raises(PermanentJobError):
        await _action(maker, _FOUND).run({})
    assert await ProposalRepo(maker).list_open(owner) == []  # nothing staged behind the gate


async def test_high_water_mark_prevents_remining(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    await _action(maker, _FOUND).run({})
    assert len(await ProposalRepo(maker).list_open(owner)) == 1
    # A second sweep with no new runs mines nothing more (the run is past the high-water mark).
    await _action(maker, _FOUND).run({})
    assert len(await ProposalRepo(maker).list_open(owner)) == 1
