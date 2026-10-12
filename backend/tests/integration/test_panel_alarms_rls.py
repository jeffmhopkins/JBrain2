"""`endpoint_panel_alarm` — the owner's alarms, a panel's own switch, a sibling's nothing.

Migration 0236. Each property is held by Postgres, not by a handler that happens to ask for one
row:

- the owner reads and writes every panel's alarms;
- a panel reads its own alarms and no other panel's;
- a panel may flip `enabled` on its own alarms and change nothing else, create nothing and
  delete nothing — the PWA is where alarms are made (owner decision);
- a sibling cannot read, switch or delete another panel's alarms.
"""

import asyncio
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


async def _alarm(maker: async_sessionmaker, sid: str, minute: int, label: str = "") -> int:
    async with scoped_session(maker, OWNER) as s:
        aid = (
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_alarm (subject_id, minute, days, label)"
                    " VALUES (CAST(:sid AS uuid), :m, 31, :label) RETURNING id"
                ),
                {"sid": sid, "m": minute, "label": label},
            )
        ).scalar_one()
        await s.commit()
    return int(aid)


async def _owner_row(maker: async_sessionmaker, aid: int) -> tuple[int, bool, str] | None:
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text("SELECT minute, enabled, label FROM app.endpoint_panel_alarm WHERE id = :id"),
                {"id": aid},
            )
        ).first()
    return None if row is None else (row[0], row[1], row[2])


async def test_a_panel_reads_its_own_alarms_and_not_its_siblings(
    maker: async_sessionmaker,
) -> None:
    mine = await _panel(maker, "panel alarm-mine")
    theirs = await _panel(maker, "panel alarm-theirs", "jpet")
    a = await _alarm(maker, mine, 405, "Work")
    await _alarm(maker, theirs, 420, "Theirs")

    async with scoped_session(maker, _as_panel(mine)) as s:
        rows = (await s.execute(text("SELECT id, label FROM app.endpoint_panel_alarm"))).all()
    assert [(r[0], r[1]) for r in rows] == [(a, "Work")]

    async with scoped_session(maker, _as_panel(theirs)) as s:
        rows = (await s.execute(text("SELECT label FROM app.endpoint_panel_alarm"))).all()
    assert [r[0] for r in rows] == ["Theirs"]


async def test_the_owner_sees_every_panel_s_alarms(maker: async_sessionmaker) -> None:
    one = await _panel(maker, "panel alarm-owner-1")
    two = await _panel(maker, "panel alarm-owner-2")
    await _alarm(maker, one, 1)
    await _alarm(maker, two, 2)
    async with scoped_session(maker, OWNER) as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.endpoint_panel_alarm"
                    " WHERE subject_id IN (CAST(:a AS uuid), CAST(:b AS uuid))"
                ),
                {"a": one, "b": two},
            )
        ).scalar_one()
    assert n == 2


async def test_a_panel_may_switch_its_own_alarm(maker: async_sessionmaker) -> None:
    mine = await _panel(maker, "panel alarm-switch")
    a = await _alarm(maker, mine, 405)
    async with scoped_session(maker, _as_panel(mine)) as s:
        n = (
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_alarm SET enabled = false"
                    " WHERE id = :id RETURNING id"
                ),
                {"id": a},
            )
        ).all()
        await s.commit()
    assert len(n) == 1
    assert await _owner_row(maker, a) == (405, False, "")


@pytest.mark.parametrize(
    "assignment",
    ["minute = 1", "days = 0", "label = 'Sneaky'", "sound = 'beeps'"],
)
async def test_a_panel_may_change_nothing_but_the_switch(
    maker: async_sessionmaker, assignment: str
) -> None:
    mine = await _panel(maker, f"panel alarm-cols-{assignment[:4]}")
    a = await _alarm(maker, mine, 405, "Work")
    async with scoped_session(maker, _as_panel(mine)) as s:
        with pytest.raises(Exception) as caught:
            await s.execute(
                text(f"UPDATE app.endpoint_panel_alarm SET {assignment} WHERE id = :id"),  # noqa: S608
                {"id": a},
            )
            await s.commit()
    assert "only switch" in str(caught.value)
    assert await _owner_row(maker, a) == (405, True, "Work")


async def test_a_panel_cannot_create_or_delete_alarms(maker: async_sessionmaker) -> None:
    mine = await _panel(maker, "panel alarm-nocreate")
    a = await _alarm(maker, mine, 405)
    async with scoped_session(maker, _as_panel(mine)) as s:
        with pytest.raises(Exception) as caught:
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_alarm (subject_id, minute)"
                    " VALUES (CAST(:sid AS uuid), 60)"
                ),
                {"sid": mine},
            )
            await s.commit()
    assert "policy" in str(caught.value).lower()

    async with scoped_session(maker, _as_panel(mine)) as s:
        gone = (
            await s.execute(
                text("DELETE FROM app.endpoint_panel_alarm WHERE id = :id RETURNING id"), {"id": a}
            )
        ).all()
        await s.commit()
    assert gone == []
    assert await _owner_row(maker, a) is not None


async def test_a_sibling_can_neither_switch_nor_delete_another_panel_s_alarm(
    maker: async_sessionmaker,
) -> None:
    desk = await _panel(maker, "panel alarm-desk")
    twin = await _panel(maker, "panel alarm-twin", "jpet")
    a = await _alarm(maker, desk, 405)
    async with scoped_session(maker, _as_panel(twin)) as s:
        switched = (
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_alarm SET enabled = false"
                    " WHERE id = :id RETURNING id"
                ),
                {"id": a},
            )
        ).all()
        deleted = (
            await s.execute(
                text("DELETE FROM app.endpoint_panel_alarm WHERE id = :id RETURNING id"), {"id": a}
            )
        ).all()
        await s.commit()
    assert (switched, deleted) == ([], [])
    assert await _owner_row(maker, a) == (405, True, "")


async def test_a_panel_cannot_move_its_alarm_onto_a_sibling(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel alarm-move")
    twin = await _panel(maker, "panel alarm-move-twin", "jpet")
    a = await _alarm(maker, desk, 405)
    async with scoped_session(maker, _as_panel(desk)) as s:
        with pytest.raises(Exception):  # noqa: B017 — the trigger or WITH CHECK, either refuses
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_alarm SET subject_id = CAST(:t AS uuid)"
                    " WHERE id = :id"
                ),
                {"t": twin, "id": a},
            )
            await s.commit()


async def test_eight_is_the_most_a_panel_holds(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel alarm-eight")
    for m in range(8):
        await _alarm(maker, desk, m)
    with pytest.raises(Exception) as caught:
        await _alarm(maker, desk, 9)
    assert "at most 8" in str(caught.value)


async def test_a_label_the_panel_cannot_draw_is_refused(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel alarm-label")
    with pytest.raises(Exception):  # noqa: B017 — the CHECK constraint
        await _alarm(maker, desk, 1, "Café")
    with pytest.raises(Exception):  # noqa: B017
        await _alarm(maker, desk, 1, "x" * 25)
    for refused in ('say "hi"', "back\\slash"):
        with pytest.raises(Exception):  # noqa: B017 — the panel's buffer has no room to escape
            await _alarm(maker, desk, 1, refused)


_INSERT = (
    "INSERT INTO app.endpoint_panel_alarm (subject_id, minute) VALUES (CAST(:sid AS uuid), :m)"
)


async def test_two_concurrent_adds_cannot_both_take_the_eighth_place(
    maker: async_sessionmaker,
) -> None:
    """Each add counts the panel's alarms before it inserts. Without the trigger's per-subject
    lock, two transactions that both count seven both land, and the panel holds nine."""
    desk = await _panel(maker, "panel alarm-race")
    for m in range(7):
        await _alarm(maker, desk, m)

    async def second() -> None:
        async with scoped_session(maker, OWNER) as s:
            await s.execute(text(_INSERT), {"sid": desk, "m": 101})
            await s.commit()

    async with scoped_session(maker, OWNER) as first:
        await first.execute(text(_INSERT), {"sid": desk, "m": 100})
        racing = asyncio.create_task(second())
        await asyncio.sleep(0.5)
        assert not racing.done(), "the second add must wait on the first one's lock"
        await first.commit()
    with pytest.raises(Exception) as caught:
        await racing
    assert "at most 8" in str(caught.value)
    async with scoped_session(maker, OWNER) as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.endpoint_panel_alarm"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": desk},
            )
        ).scalar_one()
    assert n == 8


async def test_another_panel_s_add_does_not_wait_on_this_one(maker: async_sessionmaker) -> None:
    desk = await _panel(maker, "panel alarm-lock-a")
    other = await _panel(maker, "panel alarm-lock-b")
    async with scoped_session(maker, OWNER) as held:
        await held.execute(text(_INSERT), {"sid": desk, "m": 1})
        async with scoped_session(maker, OWNER) as s:
            await asyncio.wait_for(s.execute(text(_INSERT), {"sid": other, "m": 1}), timeout=5)
            await s.commit()
        await held.commit()
