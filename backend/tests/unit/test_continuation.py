"""The plan-continuation concurrency guards (JERV_PLANNING_TOOL_PLAN.md). These are the
synchronous check-and-reserve helpers that keep a continuation from stacking on an owner
turn (live, or mid-startup) and from pushing the box past the global turn cap. Pure —
no DB, no LLM."""

import time

import pytest

from jbrain.agent.continuation import (
    _NO_CONTINUE_STOPS,
    TURN_STARTING_TTL_S,
    PlanContinuationRunner,
)


class _Marker:
    def __init__(self, session_id: str, done: bool = False) -> None:
        self.session_id = session_id
        self.done = done


def _runner(
    live_turns: dict, turn_starting: dict, *, max_concurrent: int = 4
) -> PlanContinuationRunner:
    # maker/executor/runlog/transcript are unused by the pure guard methods under test.
    return PlanContinuationRunner(
        maker=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        runlog=None,  # type: ignore[arg-type]
        transcript=None,  # type: ignore[arg-type]
        live_turns=live_turns,
        owner_principal_id=None,  # type: ignore[arg-type]
        turn_starting=turn_starting,
        max_concurrent=max_concurrent,
    )


def test_session_busy_sees_a_live_turn() -> None:
    r = _runner({"k": _Marker("s1")}, {})
    assert r._session_busy("s1")
    assert not r._session_busy("s2")


def test_a_done_turn_frees_the_session() -> None:
    r = _runner({"k": _Marker("s1", done=True)}, {})
    assert not r._session_busy("s1")


def test_a_fresh_startup_marker_blocks_a_continuation() -> None:
    # /chat sets this the instant it passes its concurrency guard, before registering in
    # live_turns — so a continuation yields to an owner turn still mid-startup.
    r = _runner({}, {"s1": time.monotonic()})
    assert r._session_busy("s1")


def test_a_stale_startup_marker_ages_out() -> None:
    # A marker a failed setup leaked clears after the TTL, so it can't wedge the loop.
    r = _runner({}, {"s1": time.monotonic() - TURN_STARTING_TTL_S - 1})
    assert not r._session_busy("s1")


def test_global_cap_blocks_when_full() -> None:
    full = {k: _Marker(f"s{i}") for i, k in enumerate("abcd")}
    assert _runner(full, {}, max_concurrent=4)._at_global_cap()
    assert not _runner({"a": _Marker("s1")}, {}, max_concurrent=4)._at_global_cap()
    # A done turn doesn't count toward the cap.
    done = {k: _Marker(f"s{i}", done=True) for i, k in enumerate("abcd")}
    assert not _runner(done, {}, max_concurrent=4)._at_global_cap()


class _NightHoldStore:
    """Minimal settings stub: the night hold is on, so a sweep must yield the box."""

    def __init__(self, held: bool) -> None:
        self._held = held

    async def night_hold_names(self, ctx: object) -> frozenset[str]:
        return frozenset({"gpt-oss-120b"}) if self._held else frozenset()


async def test_tick_yields_the_box_during_a_jmolt_night() -> None:
    # While the night hold is set, tick() returns before ANY DB work — maker=None would
    # raise the instant scoped_session touched it, so reaching the claim proves a leak.
    r = _runner({}, {})
    r.owner_principal_id = lambda: _pid("owner-1")  # type: ignore[assignment]
    r.settings_store = _NightHoldStore(held=True)  # type: ignore[assignment]
    await r.tick()  # no exception → it short-circuited before scoped_session(maker=None)


async def test_tick_proceeds_when_no_night_hold() -> None:
    # Guard the guard: with the hold clear, tick() DOES fall through to the DB (maker=None),
    # so it must raise — proving the early-return is gated on the hold, not always on.
    r = _runner({}, {})
    r.owner_principal_id = lambda: _pid("owner-1")  # type: ignore[assignment]
    r.settings_store = _NightHoldStore(held=False)  # type: ignore[assignment]
    with pytest.raises(Exception):  # noqa: B017, PT011 — any DB access on maker=None
        await r.tick()


async def _pid(pid: str) -> str:
    return pid


def test_persistent_failure_and_deferred_stops_do_not_continue() -> None:
    # A step jerv keeps failing (`too_many_errors`) must not re-arm and burn continuations;
    # a `deferred` turn already handed off to a background job.
    assert "too_many_errors" in _NO_CONTINUE_STOPS
    assert "deferred" in _NO_CONTINUE_STOPS
    assert "end_turn" not in _NO_CONTINUE_STOPS  # a clean finish DOES continue
