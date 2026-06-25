"""The AutomationsReader against real Postgres: it projects the seeded engine
config (the event triggers + reconciler/nightly schedules from migrations
0038/0040/0041/0042) into the "when -> do" cards, attaches a recent-run summary
from the runs log, and toggles a trigger/schedule's `enabled`. CLAUDE.md rule 3:
a non-owner session reads nothing and cannot toggle — the RLS firewall, not the
API, is the enforcement point."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.workflow.automations import AutomationsReader
from jbrain.workflow.registry import ACTION_SPECS, ActionRegistry, build_registry
from jbrain.workflow.scheduler import (
    PURGE_ACTION,
    RECONCILE_PENDING_INTEGRATION_ACTION,
    RECONCILE_PENDING_NOTES_ACTION,
    RECONCILE_UNEMBEDDED_NOTES_ACTION,
)
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A seeded event trigger (note.created -> event_ingest_note, migration 0040) and a
# seeded reconciler schedule-trigger (migration 0041) — stable ids the surface
# addresses across environments.
EVENT_TRIGGER = "00000000-0000-0000-0000-0000000e0001"
RECONCILE_TRIGGER = "00000000-0000-0000-0000-0000000c0012"
RECONCILE_SCHEDULE = "00000000-0000-0000-0000-0000000c0011"


def _registry() -> ActionRegistry:
    return build_registry(
        (
            *ACTION_SPECS,
            PURGE_ACTION,
            RECONCILE_PENDING_NOTES_ACTION,
            RECONCILE_PENDING_INTEGRATION_ACTION,
            RECONCILE_UNEMBEDDED_NOTES_ACTION,
        )
    )


def _reader(maker: async_sessionmaker) -> AutomationsReader:
    return AutomationsReader(maker, _registry(), frozenset(spec.name for spec in ACTION_SPECS))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_reader_projects_seeded_automations(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    view = await _reader(maker).load(owner)

    by_id = {a.trigger_id: a for a in view.automations}
    # The event trigger reads as on_event in the "event" group, not manually fireable.
    event = by_id[EVENT_TRIGGER]
    assert event.kind == "on_event"
    assert event.group == "event"
    assert event.on_event == "note.created"
    assert event.manual is False
    assert [s.action for s in event.steps] == ["ingest_note"]
    assert event.steps[0].cost_class == "standard"
    assert event.steps[0].description  # resolved through the registry

    # The reconciler reads as a manually-fireable schedule in the "reconcile" group.
    recon = by_id[RECONCILE_TRIGGER]
    assert recon.kind == "schedule"
    assert recon.group == "reconcile"
    assert recon.manual is True
    assert recon.schedule_id == RECONCILE_SCHEDULE
    assert recon.interval_seconds == 300
    assert recon.next_run_at is not None

    # A nightly sweep lands in the "nightly" group (interval 86400s).
    nightly = [a for a in view.automations if a.group == "nightly"]
    assert nightly  # the 0038 seeds exist
    assert all(a.interval_seconds == 86400 for a in nightly)


async def test_catalog_flags_seeded_vs_in_code(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    view = await _reader(maker).load(owner)
    by_name = {a.name: a for a in view.actions}
    # The shipped six are mirrored into app.actions (migration 0035).
    assert by_name["ingest_note"].seeded is True
    assert by_name["integrate_note"].seeded is True
    # The reconcilers and purge live in-code only.
    assert by_name["reconcile_pending_notes"].seeded is False
    assert by_name["purge_deleted_artifacts"].seeded is False


async def test_toggle_enabled_round_trips(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    reader = _reader(maker)

    assert await reader.set_trigger_enabled(owner, EVENT_TRIGGER, False) is True
    assert await reader.set_schedule_enabled(owner, RECONCILE_SCHEDULE, False) is True

    view = await reader.load(owner)
    by_id = {a.trigger_id: a for a in view.automations}
    assert by_id[EVENT_TRIGGER].enabled is False
    # A disabled schedule reads through on its trigger card (no next-run shown when
    # disabled is the surface's call; here we assert the stored flag took).
    async with scoped_session(maker, owner) as session:
        enabled = (
            await session.execute(
                text("SELECT enabled FROM app.schedules WHERE id = cast(:id AS uuid)"),
                {"id": RECONCILE_SCHEDULE},
            )
        ).scalar()
    assert enabled is False

    # Re-arm so the toggle is symmetric.
    assert await reader.set_trigger_enabled(owner, EVENT_TRIGGER, True) is True


async def test_toggle_unknown_id_is_false(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    reader = _reader(maker)
    assert await reader.set_trigger_enabled(owner, "not-a-uuid", True) is False
    assert (
        await reader.set_trigger_enabled(owner, "00000000-0000-0000-0000-0000deadbeef", True)
        is False
    )


async def test_reader_is_owner_only(maker: async_sessionmaker) -> None:
    await _owner(maker)
    reader = _reader(maker)
    # A scoped non-owner session: RLS hides every trigger/schedule/pipeline, so the
    # surface is empty, and a toggle finds no row to update (False).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    view = await reader.load(token)
    assert view.automations == []
    assert await reader.set_trigger_enabled(token, EVENT_TRIGGER, False) is False
    assert await reader.set_schedule_enabled(token, RECONCILE_SCHEDULE, False) is False

    # And the owner still sees the trigger enabled — the non-owner toggle was a no-op.
    owner = await _owner(maker)
    view2 = await reader.load(owner)
    by_id = {a.trigger_id: a for a in view2.automations}
    assert by_id[EVENT_TRIGGER].enabled is True
