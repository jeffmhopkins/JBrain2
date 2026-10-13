"""`endpoint_panel_setting` — the owner's requests, a panel's own report, a sibling's nothing.

Migration 0245. Each property is held by Postgres, not by a handler that asks for one row:

- the owner reads and writes every panel's row;
- a panel reads and reports into its own row and no other panel's, so the twins' panels never
  read or write the desk panel's settings;
- a panel may create its row only with nothing asked of it, may write its report freely, and may
  only DROP requests from its queue (settled or refused by its report) — never add one, change
  one or move the version;
- a panel cannot delete its row.
"""

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices import service as device_service
from jbrain.devices.repo import DeviceRole, SqlDeviceRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

ASKED = {"brightness": {"value": 60, "base": 50}, "night_color": {"value": 1, "base": 0}}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _as_panel(subject_id: str) -> SessionContext:
    return SessionContext(
        principal_id=f"key-of-{subject_id}", principal_kind="device_key", subject_id=subject_id
    )


async def _panel(maker: async_sessionmaker, name: str, role: DeviceRole = "display") -> str:
    made = await device_service.provision_or_reflash(
        SqlDeviceRepo(maker), OWNER, name, device_role=role
    )
    return made.device.id


async def _row(maker: async_sessionmaker, sid: str, desired: dict | None = None) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.endpoint_panel_setting (subject_id, desired, version, reported)"
                " VALUES (CAST(:sid AS uuid), CAST(:d AS jsonb), 3, '{\"brightness\": 50}')"
            ),
            {"sid": sid, "d": json.dumps(desired if desired is not None else ASKED)},
        )
        await s.commit()


async def _owner_view(maker: async_sessionmaker, sid: str) -> tuple[dict, int, dict] | None:
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    "SELECT desired, version, reported FROM app.endpoint_panel_setting"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": sid},
            )
        ).first()
    return None if row is None else (row[0], row[1], row[2])


async def test_a_panel_reads_its_own_row_and_never_a_sibling_s(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel set-desk")
    twin = await _panel(maker, "panel set-twin", "jpet")
    await _row(maker, desk)
    async with scoped_session(maker, _as_panel(desk)) as s:
        mine = (
            await s.execute(text("SELECT subject_id::text FROM app.endpoint_panel_setting"))
        ).all()
    assert [r[0] for r in mine] == [desk]
    async with scoped_session(maker, _as_panel(twin)) as s:
        seen = (await s.execute(text("SELECT 1 FROM app.endpoint_panel_setting"))).all()
    assert seen == []


async def test_the_owner_reads_and_writes_every_row(maker: async_sessionmaker) -> None:
    one = await _panel(maker, "panel set-owner-1")
    await _row(maker, one)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.endpoint_panel_setting SET desired = '{}', version = version + 1"
                " WHERE subject_id = CAST(:sid AS uuid)"
            ),
            {"sid": one},
        )
        await s.commit()
    assert await _owner_view(maker, one) == ({}, 4, {"brightness": 50})


async def test_a_panel_reports_and_drops_a_settled_request(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel set-report")
    await _row(maker, desk)
    async with scoped_session(maker, _as_panel(desk)) as s:
        done = (
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_setting SET reported = '{\"brightness\": 60}',"
                    " reported_at = now(), desired = CAST(:d AS jsonb) RETURNING 1"
                ),
                {"d": json.dumps({"night_color": ASKED["night_color"]})},
            )
        ).all()
        await s.commit()
    assert len(done) == 1
    assert await _owner_view(maker, desk) == (
        {"night_color": ASKED["night_color"]},
        3,
        {"brightness": 60},
    )


@pytest.mark.parametrize(
    "assignment",
    [
        # A request of its own.
        'desired = desired || \'{"volume": {"value": 10, "base": 80}}\'',
        # The owner's request, rewritten.
        'desired = \'{"brightness": {"value": 100, "base": 50}}\'',
        # The version that tells the panel a new request from an old one.
        "version = version + 1",
    ],
)
async def test_a_panel_may_only_drop_requests(maker: async_sessionmaker, assignment: str) -> None:
    desk = await _panel(maker, f"panel set-drop-{len(assignment)}")
    await _row(maker, desk)
    async with scoped_session(maker, _as_panel(desk)) as s:
        with pytest.raises(Exception) as caught:
            await s.execute(text(f"UPDATE app.endpoint_panel_setting SET {assignment}"))  # noqa: S608
            await s.commit()
    assert "only drop" in str(caught.value)
    assert await _owner_view(maker, desk) == (ASKED, 3, {"brightness": 50})


async def test_a_panel_creates_its_row_only_with_nothing_asked(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel set-create")
    async with scoped_session(maker, _as_panel(desk)) as s:
        with pytest.raises(Exception) as caught:
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_setting (subject_id, desired)"
                    " VALUES (CAST(:sid AS uuid), CAST(:d AS jsonb))"
                ),
                {"sid": desk, "d": json.dumps(ASKED)},
            )
            await s.commit()
    assert "may not ask" in str(caught.value)
    async with scoped_session(maker, _as_panel(desk)) as s:
        await s.execute(
            text(
                "INSERT INTO app.endpoint_panel_setting (subject_id, reported)"
                " VALUES (CAST(:sid AS uuid), '{\"brightness\": 50}')"
            ),
            {"sid": desk},
        )
        await s.commit()
    assert await _owner_view(maker, desk) == ({}, 0, {"brightness": 50})


async def test_a_panel_cannot_write_a_sibling_s_row_or_delete_its_own(
    maker: async_sessionmaker,
) -> None:
    desk = await _panel(maker, "panel set-sib-desk")
    twin = await _panel(maker, "panel set-sib-twin", "jpet")
    await _row(maker, desk)
    async with scoped_session(maker, _as_panel(twin)) as s:
        touched = (
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_setting SET desired = '{}', reported = '{}'"
                    " WHERE subject_id = CAST(:sid AS uuid) RETURNING 1"
                ),
                {"sid": desk},
            )
        ).all()
        gone = (
            await s.execute(
                text(
                    "DELETE FROM app.endpoint_panel_setting"
                    " WHERE subject_id = CAST(:sid AS uuid) RETURNING 1"
                ),
                {"sid": desk},
            )
        ).all()
        with pytest.raises(Exception):  # noqa: B017 — WITH CHECK: not its own subject
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_setting (subject_id)"
                    " VALUES (CAST(:sid AS uuid))"
                ),
                {"sid": desk},
            )
        await s.rollback()
    assert (touched, gone) == ([], [])
    async with scoped_session(maker, _as_panel(desk)) as s:
        mine = (await s.execute(text("DELETE FROM app.endpoint_panel_setting RETURNING 1"))).all()
        await s.commit()
    assert mine == []
    assert await _owner_view(maker, desk) == (ASKED, 3, {"brightness": 50})
