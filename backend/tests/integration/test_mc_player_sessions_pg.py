"""Migration 0224 against real Postgres: play sessions are owner-only, and the drain
turns the sidecar's join/leave events into sessions that replay and crash safely.

The drain's whole contract is in these cases:
- replaying events after an api restart writes nothing twice;
- a stopping server closes every open session;
- a crash (a new boot, with no leaves logged) closes the old boot's sessions at their
  last heartbeat;
- totals are keyed by xuid, so a gamertag change doesn't split a player.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import sessions
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
T0 = 1_791_640_000.0


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        await s.execute(text("DELETE FROM app.mc_player_sessions"))
    await engine.dispose()


class FakeSidecar:
    """`GET /events?after=` over a scripted event list, with a switchable boot."""

    def __init__(self) -> None:
        self.boot = "boot-a"
        self.events: list[dict[str, Any]] = []
        self.down = False

    def add(self, kind: str, at: float, name: str = "", xuid: str = "") -> None:
        self.events.append(
            {"id": len(self.events) + 1, "at": at, "kind": kind, "name": name, "xuid": xuid}
        )

    def restart(self, boot: str) -> None:
        self.boot, self.events = boot, []

    async def call(self, _settings: Any, _m: str, _p: str, **kw: Any) -> dict[str, Any]:
        if self.down:
            raise HTTPException(status_code=503, detail="stopped")
        after = int(kw["params"]["after"])
        return {"boot_id": self.boot, "events": [e for e in self.events if e["id"] > after]}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> FakeSidecar:
    fake = FakeSidecar()
    monkeypatch.setattr(mc, "call", fake.call)
    return fake


def _drain(maker: async_sessionmaker) -> sessions.SessionDrain:
    return sessions.SessionDrain(maker, SimpleNamespace())


async def _rows(maker: async_sessionmaker) -> Sequence[Any]:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT xuid, gamertag, extract(epoch FROM started_at) AS s,"
                    " extract(epoch FROM ended_at) AS e"
                    " FROM app.mc_player_sessions ORDER BY started_at"
                )
            )
        ).all()


async def test_join_and_leave_make_one_closed_session(maker, sidecar) -> None:
    sidecar.add("server_start", T0)
    sidecar.add("join", T0 + 10, "Steve42", "111")
    sidecar.add("leave", T0 + 610, "Steve42", "111")
    await _drain(maker).tick()
    rows = await _rows(maker)
    assert [(r.xuid, r.gamertag, r.s, r.e) for r in rows] == [("111", "Steve42", T0 + 10, T0 + 610)]


async def test_replaying_after_an_api_restart_writes_nothing_twice(maker, sidecar) -> None:
    sidecar.add("join", T0, "Steve42", "111")
    sidecar.add("leave", T0 + 60, "Steve42", "111")
    sidecar.add("join", T0 + 120, "Steve42", "111")
    await _drain(maker).tick()
    await _drain(maker).tick()  # a fresh drain re-reads from 0
    rows = await _rows(maker)
    assert [(r.s, r.e) for r in rows] == [(T0, T0 + 60), (T0 + 120, None)]


async def test_a_stopping_server_closes_every_open_session(maker, sidecar) -> None:
    sidecar.add("join", T0, "Steve42", "111")
    sidecar.add("join", T0 + 5, "Mira_P", "222")
    sidecar.add("server_stop", T0 + 100)
    await _drain(maker).tick()
    assert all(r.e == T0 + 100 for r in await _rows(maker))


async def test_a_crash_closes_the_old_boot_at_its_last_heartbeat(maker, sidecar) -> None:
    drain = _drain(maker)
    sidecar.add("join", T0, "Steve42", "111")
    await drain.tick()
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.mc_player_sessions SET last_seen_at = to_timestamp(:t)"),
            {"t": T0 + 300},
        )
    sidecar.restart("boot-b")  # no leave was ever logged
    sidecar.add("server_start", T0 + 900)
    await drain.tick()
    assert [(r.s, r.e) for r in await _rows(maker)] == [(T0, T0 + 300)]


async def test_a_stopped_sidecar_is_a_quiet_no_op(maker, sidecar) -> None:
    sidecar.down = True
    assert await _drain(maker).tick() == 0


async def test_totals_follow_the_xuid_through_a_gamertag_change(maker, sidecar) -> None:
    sidecar.add("join", T0, "OldName", "111")
    sidecar.add("leave", T0 + 100, "OldName", "111")
    sidecar.add("join", T0 + 200, "NewName", "111")
    sidecar.add("leave", T0 + 250, "NewName", "111")
    await _drain(maker).tick()
    got = await sessions.players(maker, online={})
    assert len(got) == 1
    p = got[0]
    assert (p["gamertag"], p["sessions"], p["total_seconds"]) == ("NewName", 2, 150)
    assert (p["first_seen"], p["last_seen"], p["online"]) == (T0, T0 + 250, False)


async def test_an_online_player_counts_the_live_session(maker, sidecar) -> None:
    sidecar.add("join", T0, "Steve42", "111")
    await _drain(maker).tick()
    got = await sessions.players(maker, online={"111": T0})
    assert got[0]["online"] is True and got[0]["session_started_at"] == T0
    assert got[0]["total_seconds"] > 60  # runs to now, not to the last heartbeat


async def test_the_owner_sees_sessions(maker, sidecar) -> None:
    sidecar.add("join", T0, "Steve42", "111")
    await _drain(maker).tick()
    async with scoped_session(maker, OWNER) as s:
        n = (await s.execute(text("SELECT count(*) FROM app.mc_player_sessions"))).scalar()
    assert n == 1


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_sees_nothing_and_cannot_write(maker, sidecar, ctx_name) -> None:
    ctx = {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    sidecar.add("join", T0, "Steve42", "111")
    await _drain(maker).tick()
    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT id FROM app.mc_player_sessions"))).all()
    assert rows == []
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_sessions"
                    " (xuid, gamertag, boot_id, start_event_id, started_at, last_seen_at)"
                    " VALUES ('x', 'x', 'b', 1, now(), now())"
                )
            )
