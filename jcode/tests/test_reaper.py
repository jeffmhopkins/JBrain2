"""Session GC (Wave J5): idle sessions (and their tunnels) are reaped; running and
TTL=0 are never touched. Deterministic via an injected clock."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jcode_ctl.agent import FakeCodingAgent
from jcode_ctl.app import create_app, reap_idle
from jcode_ctl.config import Settings
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


async def test_turn_that_starts_during_reap_is_not_deleted() -> None:
    """B2 (TOCTOU): ``preview.close`` is a suspension point — a turn that flips the
    session to ``running`` during it must NOT have its checkout removed mid-flight."""
    clock = Clock(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))
    mgr = _mgr(clock)
    s = await mgr.create("r")
    clock.t = clock.t + timedelta(hours=25)

    class FlipOnClosePreview(PreviewManager):
        async def close(self, sid: str) -> None:
            mgr.get(sid).status = "running"  # a queued turn starts mid-reap

    pv = FlipOnClosePreview(FakeTunnel, enabled=True)
    reaped = await reap_idle(mgr, pv, ttl_seconds=86_400)
    assert reaped == []
    assert (
        mgr.get_or_none(s.id) is not None
    )  # survived — not rmtree'd under a live turn


async def test_delete_failure_still_tears_down_the_tunnel() -> None:
    """B1 (N3 invariant): the tunnel is closed BEFORE the delete, so a delete error
    can't leave a live tunnel behind a reaped session."""
    clock = Clock(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))

    class BoomWorkspace(FakeWorkspace):
        def remove(self, path: Path) -> None:
            raise RuntimeError("rmtree failed")

    counter = {"n": 0}

    def _id() -> str:
        counter["n"] += 1
        return f"s{counter['n']}"

    mgr = SessionManager(
        FakeCodingAgent(), BoomWorkspace(), "/work", now=clock, new_id=_id
    )
    pv = PreviewManager(FakeTunnel, enabled=True)
    s = await mgr.create("r")
    await pv.open(s.id)
    clock.t = clock.t + timedelta(hours=25)

    # The delete failure propagates out of reap_idle; the loop suppresses+logs it.
    with contextlib.suppress(RuntimeError):
        await reap_idle(mgr, pv, ttl_seconds=86_400)
    assert pv.url(s.id) is None  # tunnel torn down before the failing delete


async def test_lifespan_runs_reaper_and_shuts_down_cleanly() -> None:
    """S1: entering the app lifespan starts the GC task (an idle session disappears);
    exiting it cancels the task without raising."""
    clock = Clock(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))
    mgr = _mgr(clock)
    pv = PreviewManager(FakeTunnel, enabled=True)
    settings = Settings(token="t", session_ttl_seconds=86_400, reap_interval_seconds=0)
    s = await mgr.create("r")
    clock.t = clock.t + timedelta(hours=25)

    app = create_app(settings, mgr, pv)
    async with app.router.lifespan_context(app):
        for _ in range(1000):  # let the GC task get a sweep in
            if mgr.get_or_none(s.id) is None:
                break
            await asyncio.sleep(0)
        assert mgr.get_or_none(s.id) is None
    # Context exit cancelled the reaper; reaching here means a clean shutdown.
