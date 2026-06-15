"""Migration 0035 against real Postgres: app.actions is global-read reference data
(every scope reads the seeded actions) but owner/system-write only — a scoped
non-owner principal cannot seed or edit an action (CLAUDE.md rule 3, E2). Mirrors
the app.canonical_predicates RLS precedent (0031)."""

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.workflow.registry import ACTION_SPECS
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A scoped capability token (non-owner): it reads global reference data but must not
# be able to write the actions registry.
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _action_names(maker: async_sessionmaker, ctx: SessionContext) -> set[str]:
    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT name FROM app.actions"))).scalars().all()
    return set(rows)


async def test_seeded_actions_are_globally_readable(maker: async_sessionmaker) -> None:
    """The migration seeds the six shipped actions; every scope reads them — no
    domain firewall on global machinery (the app.canonical_predicates precedent)."""
    expected = {spec.name for spec in ACTION_SPECS}
    assert await _action_names(maker, OWNER) == expected
    assert await _action_names(maker, GENERAL_ONLY) == expected
    assert await _action_names(maker, HEALTH_ONLY) == expected
    assert await _action_names(maker, UNSCOPED) == expected


async def test_seed_matches_the_in_code_registry(maker: async_sessionmaker) -> None:
    """The table is the reference projection of jbrain.workflow.registry; the seed
    must mirror the in-code specs exactly (handler, mutating, cost_class, dedup)."""
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT name, version, handler, mutating, cost_class, dedup_key_expr"
                    " FROM app.actions"
                )
            )
        ).all()
    by_name = {r.name: r for r in rows}
    for spec in ACTION_SPECS:
        row = by_name[spec.name]
        assert row.version == spec.version
        assert row.handler == spec.handler
        assert row.mutating == spec.mutating
        assert row.cost_class == spec.cost_class
        assert row.dedup_key_expr == spec.dedup_key_expr


async def test_owner_can_register_a_new_action(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.actions (name, handler) VALUES ('owner_action', 'owner_action')")
        )
    assert "owner_action" in await _action_names(maker, GENERAL_ONLY)


async def test_scoped_principal_cannot_seed_an_action(maker: async_sessionmaker) -> None:
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.actions (name, handler)"
                    " VALUES ('sneaky_action', 'sneaky_action')"
                )
            )


async def test_scoped_principal_cannot_edit_an_action(maker: async_sessionmaker) -> None:
    # The write-check fails closed: a scoped UPDATE matches no owner-writable rows
    # and silently affects nothing rather than mutating the registry.
    async with scoped_session(maker, GENERAL_ONLY) as s:
        result = await s.execute(
            text("UPDATE app.actions SET cost_class = 'cheap' WHERE name = 'integrate_note'")
        )
        assert cast(CursorResult[Any], result).rowcount == 0
    async with scoped_session(maker, OWNER) as s:
        cost = (
            await s.execute(
                text("SELECT cost_class FROM app.actions WHERE name = 'integrate_note'")
            )
        ).scalar_one()
    assert cost == "expensive"
