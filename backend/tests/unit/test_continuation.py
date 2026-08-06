"""The plan-continuation concurrency guards (JERV_PLANNING_TOOL_PLAN.md). These are the
synchronous check-and-reserve helpers that keep a continuation from stacking on an owner
turn (live, or mid-startup) and from pushing the box past the global turn cap. Pure —
no DB, no LLM."""

import time

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


def test_persistent_failure_and_deferred_stops_do_not_continue() -> None:
    # A step jerv keeps failing (`too_many_errors`) must not re-arm and burn continuations;
    # a `deferred` turn already handed off to a background job.
    assert "too_many_errors" in _NO_CONTINUE_STOPS
    assert "deferred" in _NO_CONTINUE_STOPS
    assert "end_turn" not in _NO_CONTINUE_STOPS  # a clean finish DOES continue
