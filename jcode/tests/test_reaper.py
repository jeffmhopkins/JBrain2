"""Session GC (Wave J5): idle sessions (and their tunnels) are reaped; running and
TTL=0 are never touched. Deterministic via an injected clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jcode_ctl.agent import FakeCodingAgent
from jcode_ctl.app import reap_idle
from jcode_ctl.preview import FakeTunnel, PreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import FakeWorkspace


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _mgr(clock: Clock) -> SessionManager:
    counter = {"n": 0}

    def _id() -> str:
        counter["n"] += 1
        return f"s{counter['n']}"

    return SessionManager(
        FakeCodingAgent(), FakeWorkspace(), "/work", now=clock, new_id=_id
    )


async def test_reap_idle_deletes_old_sessions_and_closes_previews() -> None:
    clock = Clock(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))
    mgr = _mgr(clock)
    pv = PreviewManager(FakeTunnel, enabled=True)

    old = await mgr.create("r")
    await pv.open(old.id)

    clock.t = clock.t + timedelta(hours=25)  # 25h passes
    fresh = await mgr.create("r")  # created at the new time

    reaped = await reap_idle(mgr, pv, ttl_seconds=86_400)
    assert reaped == [old.id]
    assert pv.url(old.id) is None  # the tunnel was torn down with the session
    assert mgr.get(fresh.id).id == fresh.id  # the fresh one survives


async def test_ttl_zero_disables_reaping() -> None:
    clock = Clock(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))
    mgr = _mgr(clock)
    await mgr.create("r")
    clock.t = clock.t + timedelta(days=30)
    assert mgr.idle_sessions(ttl_seconds=0) == []


async def test_running_session_is_never_reaped() -> None:
    clock = Clock(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))
    mgr = _mgr(clock)
    s = await mgr.create("r")
    mgr.get(s.id).status = "running"  # an in-flight turn
    clock.t = clock.t + timedelta(days=2)
    assert mgr.idle_sessions(ttl_seconds=86_400) == []
