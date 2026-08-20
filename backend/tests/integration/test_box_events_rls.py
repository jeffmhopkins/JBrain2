"""Migration 0167 against real Postgres: `box_events` is owner-only (CLAUDE.md rule 3),
and the recorder's round trip holds.

The mandatory per-new-table RLS isolation test, plus the two behaviours the vitals surface
depends on and cannot get from a unit test: an event opened but not yet ended is READ BACK
as still running (that is what puts "loading gpt-oss-120b…" on screen during the spike),
and a still-open event started before the window still shows up in it — a load that began
three minutes ago is the explanation for the last three minutes of trace.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import box_events
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def wired(maker: async_sessionmaker) -> AsyncIterator[async_sessionmaker]:
    """The writer wired the way a real process wires it, and unwired afterwards — it is
    module-global, so a leak would have the rest of the suite writing to a dead engine.

    The table is emptied first: these tests share one container, and a window read is only
    meaningful against a known set of rows."""
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        await session.execute(text("DELETE FROM app.box_events"))
    box_events.configure(maker, source="api")
    yield maker
    box_events.reset()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_a_load_is_readable_while_it_is_still_happening(
    wired: async_sessionmaker,
) -> None:
    owner = await _owner(wired)

    async with box_events.span(box_events.MODEL_LOAD, "gpt-oss-120b"):
        during = await box_events.recent(wired, owner, seconds=900)
        assert [(e["subject"], e["status"], e["ended_ms"]) for e in during] == [
            ("gpt-oss-120b", "running", None)
        ]

    after = await box_events.recent(wired, owner, seconds=900)
    assert after[0]["status"] == "ok"
    assert after[0]["ended_ms"] is not None


async def test_an_eviction_carries_the_reason_it_happened(wired: async_sessionmaker) -> None:
    owner = await _owner(wired)

    with box_events.because("to make room for gpt-oss-120b"):
        await box_events.record(box_events.MODEL_UNLOAD, "qwen35")

    events = await box_events.recent(wired, owner, seconds=900)
    assert events[0]["detail"] == "to make room for gpt-oss-120b"
    assert events[0]["source"] == "api"


async def test_a_load_that_started_before_the_window_still_explains_it(
    wired: async_sessionmaker,
) -> None:
    """A 90-second load is why the last minute of trace is pinned, so it belongs in a
    one-minute window even though it started outside it."""
    owner = await _owner(wired)
    started = datetime.now(tz=UTC) - timedelta(seconds=180)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, kind, subject, status, source) "
                "VALUES (:at, 'model_load', 'gpt-oss-120b', 'running', 'worker')"
            ),
            {"at": started},
        )

    events = await box_events.recent(wired, owner, seconds=60)
    assert [e["subject"] for e in events] == ["gpt-oss-120b"]


async def test_a_load_that_finished_inside_the_window_stays_in_it(
    wired: async_sessionmaker,
) -> None:
    """The regression: selecting on START alone made a 90-second load VANISH from the 1m
    range at the instant it settled — the row went from "loading gpt-oss-120b…" straight to
    gone, rather than to "loaded gpt-oss-120b", while the spike it caused was still drawn
    above it."""
    owner = await _owner(wired)
    started = datetime.now(tz=UTC) - timedelta(seconds=90)
    ended = datetime.now(tz=UTC) - timedelta(seconds=10)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, ended_at, kind, subject, status, source) "
                "VALUES (:at, :end, 'model_load', 'gpt-oss-120b', 'ok', 'api')"
            ),
            {"at": started, "end": ended},
        )

    events = await box_events.recent(wired, owner, seconds=60)
    assert [(e["subject"], e["status"]) for e in events] == [("gpt-oss-120b", "ok")]


async def test_settled_events_leave_the_window(wired: async_sessionmaker) -> None:
    owner = await _owner(wired)
    old = datetime.now(tz=UTC) - timedelta(seconds=3600)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, ended_at, kind, subject, status, source) "
                "VALUES (:at, :at, 'model_unload', 'qwen35', 'ok', 'api')"
            ),
            {"at": old},
        )

    assert await box_events.recent(wired, owner, seconds=900) == []


async def test_a_row_abandoned_by_a_dead_process_ages_out(wired: async_sessionmaker) -> None:
    """A process restarted mid-load leaves its row open forever. It must not sit at the top
    of the list claiming to still be loading."""
    owner = await _owner(wired)
    abandoned = datetime.now(tz=UTC) - box_events.STALE_AFTER - timedelta(minutes=1)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, kind, subject, status, source) "
                "VALUES (:at, 'model_load', 'gpt-oss-120b', 'running', 'api')"
            ),
            {"at": abandoned},
        )

    # Inside a window wide enough to hold it, it reads as stale — never as still loading.
    wide = await box_events.recent(wired, owner, seconds=3600)
    assert [e["status"] for e in wide] == ["stale"]
    # Outside that window it is simply gone, rather than pinned to the top forever.
    assert await box_events.recent(wired, owner, seconds=60) == []


async def test_in_flight_reports_the_load_happening_right_now(
    wired: async_sessionmaker,
) -> None:
    """The narrow read behind the chat status line: one row, present tense, or nothing.

    Distinct from `recent` because a status line has room for one thing, not a window: it
    needs "is the box loading, and what" answered in the affirmative only while it is true."""
    owner = await _owner(wired)

    assert await box_events.in_flight(wired, owner) is None

    async with box_events.span(box_events.MODEL_LOAD, "gpt-oss-120b"):
        during = await box_events.in_flight(wired, owner)
        assert during is not None
        assert during["subject"] == "gpt-oss-120b"
        assert during["source"] == "api"

    # Settled: the status line falls silent the moment the weights are in, rather than
    # holding "loading…" until some window scrolls past it.
    assert await box_events.in_flight(wired, owner) is None


async def test_in_flight_ignores_work_of_another_kind(wired: async_sessionmaker) -> None:
    """An image render pins the GPU just as hard, but "Loading …" is a claim about a MODEL
    and the line must not make it about a render."""
    owner = await _owner(wired)

    async with box_events.span(box_events.IMAGE_RENDER, "sdxl"):
        assert await box_events.in_flight(wired, owner) is None


async def test_in_flight_falls_silent_on_a_row_a_dead_process_left_open(
    wired: async_sessionmaker,
) -> None:
    """The one lie a status line cannot afford. A process killed mid-load leaves its row
    open forever; the vitals LIST can render that honestly as "stopped reporting", but a
    single line has no way to say it — so it says nothing instead of "loading gpt-oss-120b…"
    for the rest of the session."""
    owner = await _owner(wired)
    abandoned = datetime.now(tz=UTC) - box_events.STALE_AFTER - timedelta(minutes=1)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, kind, subject, status, source) "
                "VALUES (:at, 'model_load', 'gpt-oss-120b', 'running', 'api')"
            ),
            {"at": abandoned},
        )

    assert await box_events.in_flight(wired, owner) is None


async def test_in_flight_prefers_the_newer_of_two_open_rows(wired: async_sessionmaker) -> None:
    """Only one model loads at a time, but a row abandoned inside the staleness window can
    still be open beside the real one. The newer row is the true one."""
    owner = await _owner(wired)
    stalled = datetime.now(tz=UTC) - timedelta(minutes=5)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, kind, subject, status, source) "
                "VALUES (:at, 'model_load', 'qwen35', 'running', 'worker')"
            ),
            {"at": stalled},
        )
    await box_events._write(  # noqa: SLF001 — the open-row insert, without a span to close it
        box_events._INSERT_OPEN,  # noqa: SLF001
        {
            "id": uuid.uuid4(),
            "kind": box_events.MODEL_LOAD,
            "subject": "gpt-oss-120b",
            "detail": "",
            "source": "api",
        },
    )

    live = await box_events.in_flight(wired, owner)
    assert live is not None
    assert live["subject"] == "gpt-oss-120b"


async def test_prune_drops_rows_past_retention(wired: async_sessionmaker) -> None:
    owner = await _owner(wired)
    old = datetime.now(tz=UTC) - box_events.RETENTION - timedelta(hours=1)

    async with scoped_session(wired, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.box_events (at, ended_at, kind, subject) "
                "VALUES (:at, :at, 'model_load', 'ancient')"
            ),
            {"at": old},
        )
    await box_events.record(box_events.MODEL_LOAD, "recent")

    assert await box_events.prune(wired, owner) == 1
    async with scoped_session(wired, owner) as session:
        left = (await session.execute(text("SELECT subject FROM app.box_events"))).scalars().all()
    assert left == ["recent"]


async def test_non_owner_sees_nothing_and_cannot_write(wired: async_sessionmaker) -> None:
    owner = await _owner(wired)
    await box_events.record(box_events.MODEL_LOAD, "gpt-oss-120b")

    # A non-owner principal sees zero rows — RLS hides the whole table.
    async with scoped_session(wired, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.box_events"))).scalar()
    assert count == 0
    assert await box_events.recent(wired, NON_OWNER, seconds=900) == []
    assert owner.principal_kind == "owner"

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(wired, NON_OWNER) as session:
            await session.execute(
                text("INSERT INTO app.box_events (kind, subject) VALUES ('model_load', 'x')")
            )
