"""The appointment projector against real Postgres, driven through the genuine
analyze_note pipeline (and the note-deletion purge).

An appointment is a projection of its appointment entity's current facts. These
scenarios assert the table mirrors the graph: a reschedule moves the one row, two
distinct appointments make two rows, a restatement stays one, a dropped time
removes the row, and a purge reverts/cascades the projection.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.appointment_projection import project_appointments
from jbrain.analysis.purge import purge_note_artifacts
from tests.conftest import docker_available
from tests.harness.runner import run_scenario
from tests.harness.scenario import SCENARIOS_DIR, Scenario, Step, load_scenario
from tests.integration.test_rls import APP_PASSWORD, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def admin_maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    engine: AsyncEngine = create_async_engine(admin_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(admin_maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Each test starts from an empty graph. Truncating entities CASCADEs to the
    appointments projection (the entity_id FK), so the table clears with it."""
    async with admin_maker() as s:
        await s.execute(
            text(
                "TRUNCATE app.facts, app.entities, app.entity_mentions, app.entity_aliases,"
                " app.temporal_tokens, app.review_items, app.note_analysis,"
                " app.chunks, app.notes, app.appointments CASCADE"
            )
        )
        await s.commit()
    yield


async def _appointments(maker: async_sessionmaker) -> list[dict]:
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT title, starts_at, ends_at, all_day, status, rrule, domain_code,"
                        " source_note_id::text AS source_note_id FROM app.appointments"
                        " ORDER BY starts_at"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _file(name: str) -> Scenario:
    return load_scenario(SCENARIOS_DIR / name)


async def test_reschedule_projects_one_current_time(maker: async_sessionmaker) -> None:
    await run_scenario(maker, _file("plan_appointment_reschedule.json"))
    rows = await _appointments(maker)
    # One appointment entity, one row — at the rescheduled (Monday) time, not the
    # superseded Friday one. Status defaults to confirmed; the note is attributed.
    assert len(rows) == 1
    appt = rows[0]
    assert appt["starts_at"] == datetime(2026, 6, 15, 20, 0, tzinfo=UTC)  # 14:00-06:00
    assert appt["status"] == "confirmed"
    assert appt["domain_code"] == "general"
    assert appt["source_note_id"] is not None


async def test_two_distinct_appointments_make_two_rows(maker: async_sessionmaker) -> None:
    await run_scenario(maker, _file("plan_two_distinct_appointments.json"))
    rows = await _appointments(maker)
    starts = [r["starts_at"] for r in rows]
    assert starts == [
        datetime(2026, 6, 20, 15, 0, tzinfo=UTC),  # dentist 09:00-06:00
        datetime(2026, 6, 25, 21, 0, tzinfo=UTC),  # optometrist 15:00-06:00
    ]


async def test_idempotent_restatement_stays_one_row(maker: async_sessionmaker) -> None:
    await run_scenario(maker, _file("plan_idempotent_restatement.json"))
    rows = await _appointments(maker)
    assert len(rows) == 1
    assert rows[0]["starts_at"] == datetime(2026, 6, 11, 16, 0, tzinfo=UTC)  # 10:00-06:00


async def test_dropped_scheduled_time_removes_the_row(maker: async_sessionmaker) -> None:
    create = {
        "title": "Standup",
        "tags": ["standup"],
        "mentions": [
            {"name": "team standup", "kind": "appointment", "surface_text": "team standup"}
        ],
        "facts": [
            {
                "predicate": "scheduledTime",
                "qualifier": "",
                "kind": "state",
                "statement": "Team standup Thursday 10am.",
                "value_json": {"start": "2026-06-11T10:00:00-06:00"},
                "assertion": "expected",
                "entity_ref": "team standup",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "Thursday 10am",
                    "resolved_start": "2026-06-11T10:00:00-06:00",
                    "resolved_end": None,
                    "precision": "instant",
                },
                "domain": "general",
                "confidence": 0.9,
            }
        ],
        "temporal_tokens": [],
    }
    # The same note re-read as no longer asserting the appointment: the sweep
    # retracts its scheduledTime, so the projection has no live time to show.
    dropped = {"title": "", "tags": [], "mentions": [], "facts": [], "temporal_tokens": []}
    scenario = Scenario(
        name="dropped",
        steps=[
            Step(body="Team standup is Thursday at 10am.", extraction=create),
            Step(body="(edited)", extraction=dropped, reanalyze_step=0),
        ],
        expect={},
    )
    await run_scenario(maker, scenario)
    assert await _appointments(maker) == []


async def test_purge_cascades_the_orphaned_appointment(maker: async_sessionmaker) -> None:
    await run_scenario(maker, _file("plan_two_distinct_appointments.json"))
    rows = await _appointments(maker)
    assert len(rows) == 2
    # Each appointment came from its own note; purging the dentist's note orphans
    # that entity, which cascades its projection away — the optometrist remains.
    dentist = min(rows, key=lambda r: r["starts_at"])
    await _purge(maker, dentist["source_note_id"])
    remaining = await _appointments(maker)
    assert len(remaining) == 1
    assert remaining[0]["starts_at"] == datetime(2026, 6, 25, 21, 0, tzinfo=UTC)


async def test_direct_projection_carries_rrule_and_explicit_status(
    maker: async_sessionmaker,
) -> None:
    # Build a recurring, tentative appointment directly (a recurrence token's
    # RRULE on the scheduledTime fact, plus an explicit status fact), then run the
    # projector — the deterministic way to cover the rrule join and status branch.
    start = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    rrule = "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    eid = uuid.uuid4()
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        note_id = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
                " VALUES (:i, :c, 'general', 'gym', :t)"
            ),
            {"i": str(note_id), "c": str(note_id)[:12], "t": start},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:i, 'appointment', 'Gym session', 'general')"
            ),
            {"i": str(eid)},
        )
        tok = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO app.temporal_tokens (id, note_id, surface_phrase, kind,"
                " resolved_start, temporal_precision, capture_anchor, rrule, domain_code)"
                " VALUES (:i, :n, 'every MWF', 'recurrence', :t, 'instant', :t, :r, 'general')"
            ),
            {"i": str(tok), "n": str(note_id), "t": start, "r": rrule},
        )
        common = {"n": str(note_id), "e": str(eid)}
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
                " assertion, valid_from, reported_at, temporal_token_id, note_id, extractor,"
                " prompt_version, domain_code) VALUES (gen_random_uuid(), :e, 'scheduledTime',"
                " 'state', 'Gym session recurring', :v, 'expected', :t, :t, :tok, :n, 'x', '1',"
                " 'general')"
            ),
            {**common, "v": '{"start": "2026-07-01T16:00:00+00:00"}', "t": start, "tok": str(tok)},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
                " assertion, reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'status', 'state', 'Tentative', :v, 'asserted',"
                " :t, :n, 'x', '1', 'general')"
            ),
            {**common, "v": '{"value": "tentative"}', "t": start},
        )
        await project_appointments(s, {eid})
        await s.commit()

    rows = await _appointments(maker)
    assert len(rows) == 1
    assert rows[0]["rrule"] == rrule
    assert rows[0]["status"] == "tentative"
    assert rows[0]["starts_at"] == start


async def _seed_appointment(s, *, precision: str = "instant") -> tuple[uuid.UUID, uuid.UUID]:
    """A note + appointment entity + one active scheduledTime fact. Returns
    (entity_id, note_id). Runs on an owner session `s`."""
    start = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    eid, note_id = uuid.uuid4(), uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
            " VALUES (:i, :c, 'general', 'x', :t)"
        ),
        {"i": str(note_id), "c": str(note_id)[:12], "t": start},
    )
    await s.execute(
        text(
            "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
            " VALUES (:i, 'appointment', 'Visit', 'general')"
        ),
        {"i": str(eid)},
    )
    await s.execute(
        text(
            "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
            " assertion, valid_from, reported_at, temporal_precision, note_id, extractor,"
            " prompt_version, domain_code) VALUES (gen_random_uuid(), :e, 'scheduledTime',"
            " 'state', 'Visit', :v, 'expected', :t, :t, :p, :n, 'x', '1', 'general')"
        ),
        {
            "e": str(eid),
            "n": str(note_id),
            "t": start,
            "p": precision,
            "v": '{"start": "2026-07-01T16:00:00+00:00"}',
        },
    )
    return eid, note_id


async def test_recurrence_rides_a_separate_recurrence_fact(maker: async_sessionmaker) -> None:
    # The real shape: recurrence is its OWN `recurrence` predicate fact with a
    # recurrence-kind token (facets.yaml), NOT the scheduledTime token. The
    # projector must still pick up the RRULE.
    rrule = "FREQ=WEEKLY;BYDAY=TU"
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        eid, note_id = await _seed_appointment(s)
        tok = uuid.uuid4()
        start = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
        await s.execute(
            text(
                "INSERT INTO app.temporal_tokens (id, note_id, surface_phrase, kind,"
                " resolved_start, temporal_precision, capture_anchor, rrule, domain_code)"
                " VALUES (:i, :n, 'every Tuesday', 'recurrence', :t, 'instant', :t, :r, 'general')"
            ),
            {"i": str(tok), "n": str(note_id), "t": start, "r": rrule},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
                " assertion, valid_from, reported_at, temporal_token_id, note_id, extractor,"
                " prompt_version, domain_code) VALUES (gen_random_uuid(), :e, 'recurrence',"
                " 'state', 'weekly', NULL, 'expected', :t, :t, :tok, :n, 'x', '1', 'general')"
            ),
            {"e": str(eid), "n": str(note_id), "t": start, "tok": str(tok)},
        )
        await project_appointments(s, {eid})
        await s.commit()

    rows = await _appointments(maker)
    assert len(rows) == 1 and rows[0]["rrule"] == rrule


async def test_day_precision_projects_as_all_day(maker: async_sessionmaker) -> None:
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        await project_appointments(s, set())  # empty set is a no-op
        eid, _ = await _seed_appointment(s, precision="day")
        await project_appointments(s, {eid})
        await s.commit()
    rows = await _appointments(maker)
    assert len(rows) == 1 and rows[0]["all_day"] is True


async def test_cancelled_appointment_keeps_its_row_when_the_time_is_gone(
    maker: async_sessionmaker,
) -> None:
    # A cancellation whose note no longer asserts a time must NOT delete the row —
    # the feed has to keep emitting STATUS:CANCELLED so subscribers remove it.
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        eid, note_id = await _seed_appointment(s)
        await project_appointments(s, {eid})  # row exists, confirmed
        # Retract the scheduledTime, add an active cancelled status, re-project.
        await s.execute(
            text("UPDATE app.facts SET status='retracted' WHERE entity_id = :e"),
            {"e": str(eid)},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
                " assertion, reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'status', 'state', 'Cancelled',"
                " '{\"value\": \"cancelled\"}', 'asserted', now(), :n, 'x', '1', 'general')"
            ),
            {"e": str(eid), "n": str(note_id)},
        )
        await project_appointments(s, {eid})
        await s.commit()

    rows = await _appointments(maker)
    assert len(rows) == 1 and rows[0]["status"] == "cancelled"


async def test_active_time_with_no_resolvable_start_removes_row(
    maker: async_sessionmaker,
) -> None:
    # An active scheduledTime fact carrying neither a value_json start nor a
    # valid_from has nothing to put on a calendar — the projection row is removed.
    eid = uuid.uuid4()
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        note_id = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
                " VALUES (:i, :c, 'general', 'x', now())"
            ),
            {"i": str(note_id), "c": str(note_id)[:12]},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:i, 'appointment', 'Timeless', 'general')"
            ),
            {"i": str(eid)},
        )
        # A stale projection row exists; the timeless fact must clear it.
        await s.execute(
            text(
                "INSERT INTO app.appointments (domain_code, entity_id, title, starts_at)"
                " VALUES ('general', :e, 'Timeless', now())"
            ),
            {"e": str(eid)},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
                " assertion, reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'scheduledTime', 'state', 'no time', '{}',"
                " 'expected', now(), :n, 'x', '1', 'general')"
            ),
            {"e": str(eid), "n": str(note_id)},
        )
        await project_appointments(s, {eid})
        await s.commit()

    assert await _appointments(maker) == []


async def _purge(maker: async_sessionmaker, note_id: str) -> None:
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        await purge_note_artifacts(s, uuid.UUID(note_id))
        await s.commit()
