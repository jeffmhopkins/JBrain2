"""The API's recovery path — the one that does not run through the API.

`restart: unless-stopped` covers a process that EXITS. The failure this exists for is a
process still running and still holding its socket while serving nothing: the API's own
metrics sampler recorded a 328-second gap while the edge answered 530, and docker saw a
healthy container throughout.
"""

import asyncio
import contextlib

import pytest

from supervisor.gateway import UpdateStatus
from supervisor.watchdog import watch_api

pytestmark = pytest.mark.asyncio


class FakeGateway:
    """Models the REAL label split, which is the point of this double.

    The updater carries `jbrain.updater=1` and is read through `update_status`; only
    export/import/reset/provision/rebuild carry `jbrain.oneshot=<kind>` and answer to
    `oneshot_status`. An earlier version of this fake answered "running" from
    `oneshot_status` for EVERY kind including "update", so the guard's tests passed
    while the real gateway reported nothing and a restart could land mid-migration."""

    def __init__(self, *, updater: str = "none", oneshot: str = "none") -> None:
        self.restarted: list[str] = []
        self.updater = updater
        self.oneshot = oneshot

    def restart(self, service: str) -> None:
        self.restarted.append(service)

    def update_status(self, tail: int) -> UpdateStatus:
        return UpdateStatus(state=self.updater, exit_code=None, log_tail="")

    def oneshot_status(self, kind: str, tail: int) -> UpdateStatus:
        # "update" is NOT a oneshot kind. The real gateway does not complain about
        # it: `_latest("jbrain.oneshot=update")` matches nothing and the status
        # reads "none". Modelled as that quiet no rather than an exception on
        # purpose — raising is swallowed by the fail-closed path, which would make
        # the buggy version look correct. That is how the defect hid the first time.
        if kind == "update":
            return UpdateStatus(state="none", exit_code=None, log_tail="")
        return UpdateStatus(state=self.oneshot, exit_code=None, log_tail="")


def probes(results: list[bool]):
    """A probe that yields `results` then ends the loop, so a test terminates on the
    reads it cares about rather than on a timer."""
    it = iter(results)

    def probe(url: str, timeout: float) -> bool:
        try:
            return next(it)
        except StopIteration:
            raise asyncio.CancelledError from None

    return probe


async def _run(gateway, probe, **kw) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await watch_api(gateway, probe=probe, interval=0, **kw)


async def test_restarts_the_api_after_enough_consecutive_silence() -> None:
    gateway = FakeGateway()

    await _run(gateway, probes([False] * 3), failures_before_restart=3)

    assert gateway.restarted == ["api"]


async def test_a_single_slow_probe_is_not_a_restart() -> None:
    """One failed probe during a model load is ordinary. Only sustained silence
    counts."""
    gateway = FakeGateway()

    await _run(
        gateway, probes([True, False, True, False, True]), failures_before_restart=3
    )

    assert gateway.restarted == []


async def test_the_run_up_resets_when_the_api_answers() -> None:
    gateway = FakeGateway()

    await _run(
        gateway, probes([False, False, True, False, False]), failures_before_restart=3
    )

    assert gateway.restarted == []


async def test_never_restarts_while_an_update_is_running() -> None:
    """An update stops and recreates the api container on purpose; restarting it mid-run
    would fight the updater and could leave the box half-migrated.

    The updater is found via `update_status`, NOT `oneshot_status("update")`: that kind
    does not exist, so asking for it returns a quiet "none" and the guard would pass."""
    gateway = FakeGateway(updater="running")

    await _run(gateway, probes([False] * 6), failures_before_restart=3)

    assert gateway.restarted == []


async def test_never_restarts_while_another_oneshot_is_running() -> None:
    """A restore or a provision stops the stack just as deliberately as an update."""
    gateway = FakeGateway(oneshot="running")

    await _run(gateway, probes([False] * 6), failures_before_restart=3)

    assert gateway.restarted == []


async def test_an_unreadable_updater_state_counts_as_busy() -> None:
    """Fail closed on the updater specifically, not only on the other one-shots."""

    class Unreadable(FakeGateway):
        def update_status(self, tail: int) -> UpdateStatus:
            raise RuntimeError("docker unreachable")

    gateway = Unreadable()

    await _run(gateway, probes([False] * 6), failures_before_restart=3)

    assert gateway.restarted == []


async def test_an_unreadable_oneshot_state_counts_as_busy() -> None:
    """Fail closed: the outcome to avoid is restarting DURING an update, so not knowing
    must not read as permission to act."""

    class Unreadable(FakeGateway):
        def oneshot_status(self, kind: str, tail: int) -> UpdateStatus:
            raise RuntimeError("docker unreachable")

    gateway = Unreadable()

    await _run(gateway, probes([False] * 6), failures_before_restart=3)

    assert gateway.restarted == []


async def test_holds_off_a_second_restart_inside_the_cooldown() -> None:
    """A crash-looping API must surface as a crash-loop, not be restarted every minute —
    that masks the fault and adds load to a box that is already unwell."""
    gateway = FakeGateway()
    ticks = iter([0.0] * 20)

    await _run(
        gateway,
        probes([False] * 9),
        failures_before_restart=3,
        cooldown=300.0,
        clock=lambda: next(ticks),
    )

    assert gateway.restarted == ["api"]


async def test_restarts_again_once_the_cooldown_has_passed() -> None:
    gateway = FakeGateway()
    now = iter([0.0, 1000.0, 2000.0, 3000.0, 4000.0])

    await _run(
        gateway,
        probes([False] * 6),
        failures_before_restart=3,
        cooldown=300.0,
        clock=lambda: next(now),
    )

    assert gateway.restarted == ["api", "api"]


async def test_a_failing_probe_cycle_never_kills_the_watchdog() -> None:
    """Nothing is left watching if this task dies, and nothing would say so."""
    calls = {"n": 0}

    def probe(url: str, timeout: float) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        if calls["n"] > 3:
            raise asyncio.CancelledError
        return True

    gateway = FakeGateway()

    await _run(gateway, probe)

    assert calls["n"] > 3  # kept going after the exception
