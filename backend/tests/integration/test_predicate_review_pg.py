"""The `predicate_review` action (Loop 3a, Wave 2) end-to-end against real Postgres: open
new_predicate cards are turned into an owner `predicate-canon` proposal (NEVER auto-resolved);
enacting it runs the shipped resolve_review (map heals + writes the durable alias; cold mints); a
re-run skips cards already proposed (idempotent); the kill-switch refuses. The proposal is the
owner gate — nothing changes until approval."""

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.connectortools import build_leaf_executor
from jbrain.agent.predicatereview import PredicateReviewAction
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.session import read_context
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import SELF_IMPROVEMENT_KILL_SWITCH_KEY, SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _isolate(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    """The module-scoped DB is shared; truncate the tables each test mutates so cards/proposals
    don't leak (the idempotency test counts proposals). Admin role — the app role lacks DELETE."""
    yield
    admin = create_async_engine(
        database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test"), poolclass=NullPool
    )
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE app.review_items, app.proposals, app.canonical_predicates,"
                    " app.predicate_aliases, app.settings RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await admin.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind='owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _seed_canonical(maker: async_sessionmaker, name: str) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind)"
                " VALUES (:n, 'd', 'ref', 'relationship') ON CONFLICT (canonical_name) DO NOTHING"
            ),
            {"n": name},
        )


async def _seed_card(
    maker: async_sessionmaker,
    predicate: str,
    suggestions: list[list[Any]],
    *,
    domain: str = "general",
) -> str:
    payload = {
        "predicate": predicate,
        "fact_kind": "relationship",
        "statement": f"x {predicate} y",
        "suggestions": suggestions,
    }
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                    " VALUES (gen_random_uuid(), 'new_predicate', cast(:p AS jsonb), :d)"
                    " RETURNING id::text"
                ),
                {"p": json.dumps(payload), "d": domain},
            )
        ).scalar_one()


async def _card_status(maker: async_sessionmaker, card_id: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT status FROM app.review_items WHERE id = :i"), {"i": card_id}
            )
        ).scalar_one()


async def _aliases(maker: async_sessionmaker) -> dict[str, str]:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(text("SELECT raw_norm, canonical_name FROM app.predicate_aliases"))
        ).all()
    return {r.raw_norm: r.canonical_name for r in rows}


async def _proposal_count(maker: async_sessionmaker, owner: SessionContext) -> int:
    return len(await ProposalRepo(maker).list_open(owner))


def _action(maker: async_sessionmaker) -> PredicateReviewAction:
    return PredicateReviewAction(
        maker, settings=SqlSettingsStore(maker), proposals=ProposalRepo(maker)
    )


async def test_stages_owner_proposal_then_enact_resolves_cards(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_canonical(maker, "spouse")
    mappable = await _seed_card(maker, "zzqMarriedTo", [["spouse", 0.71]])
    novel = await _seed_card(maker, "zzqFrobnicates", [])

    await _action(maker).run({})

    # ONE owner proposal (both cards are 'general'); the cards are still OPEN (not auto-resolved).
    proposals = ProposalRepo(maker)
    open_props = await proposals.list_open(owner)
    assert [p.kind for p in open_props] == ["predicate-canon"]
    assert await _card_status(maker, mappable) == "open"
    assert await _card_status(maker, novel) == "open"

    # Owner approves every leaf + enacts → resolve_review runs per leaf.
    prop_id = open_props[0].id
    _proposal, nodes = await proposals.load(owner, prop_id)
    for node in nodes:
        await proposals.decide(owner, node.id, approve=True)
    none: Any = cast(Any, None)
    executor = build_leaf_executor(none, none, none, SqlAnalysisRepo(maker), none)
    await proposals.enact(owner, prop_id, executor)

    # The mappable card mapped onto spouse (durable alias recorded); the novel one minted.
    assert await _card_status(maker, mappable) != "open"
    assert await _card_status(maker, novel) != "open"
    assert (await _aliases(maker)).get("zzqmarriedto") == "spouse"


async def test_rerun_is_idempotent(maker: async_sessionmaker) -> None:
    await _owner(maker)
    await _seed_canonical(maker, "spouse")
    await _seed_card(maker, "zzqMarriedTo", [["spouse", 0.71]])
    owner = await _owner(maker)

    await _action(maker).run({})
    assert await _proposal_count(maker, owner) == 1
    # A second sweep finds the card already carried by a staged proposal → stages nothing more.
    await _action(maker).run({})
    assert await _proposal_count(maker, owner) == 1


async def test_proposals_are_domain_firewalled(maker: async_sessionmaker) -> None:
    # One proposal per domain (predicatereview groups by the card's domain). A narrowed session
    # only ever sees the proposal in a domain it holds (non-neg #3 — the card-batch/proposal path).
    owner = await _owner(maker)
    pid = str(owner.principal_id)
    await _seed_card(maker, "zzqGeneralPred", [], domain="general")
    await _seed_card(maker, "zzqHealthPred", [], domain="health")

    await _action(maker).run({})

    proposals = ProposalRepo(maker)
    general_only = await proposals.list_open(read_context(pid, ("general",)))
    health_only = await proposals.list_open(read_context(pid, ("health",)))
    assert [p.domain for p in general_only] == ["general"]  # health proposal is firewalled out
    assert [p.domain for p in health_only] == ["health"]


async def test_refused_when_kill_switch_on(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_card(maker, "zzqFrobnicates", [])
    await SqlSettingsStore(maker).upsert(owner, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    from jbrain.queue import PermanentJobError

    with pytest.raises(PermanentJobError):
        await _action(maker).run({})
    assert await _proposal_count(maker, owner) == 0  # nothing staged behind the gate
