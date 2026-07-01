"""The guided-intake reaper loop wiring (W3)."""

import asyncio

import pytest

from jbrain.db.session import SessionContext
from jbrain.intake.sweep import intake_reaper_loop


class _Repo:
    """Records the reap window; raises CancelledError on the first call to break the loop
    deterministically (no real clock — CancelledError is not caught by `except Exception`)."""

    def __init__(self) -> None:
        self.windows: list[int] = []

    async def reap_abandoned(self, ctx: SessionContext, older_than_seconds: int) -> int:
        self.windows.append(older_than_seconds)
        raise asyncio.CancelledError


async def test_reaper_loop_invokes_reap_with_the_window() -> None:
    repo = _Repo()
    with pytest.raises(asyncio.CancelledError):
        await intake_reaper_loop(
            repo,  # type: ignore[arg-type]
            SessionContext(principal_kind="owner"),
            interval_seconds=999,
            older_than_seconds=7200,
        )
    assert repo.windows == [7200]


async def test_reaper_loop_survives_a_sweep_error() -> None:
    """A transient sweep failure is swallowed so the loop keeps running; the second call
    cancels to end the test."""
    calls = 0

    class _Flaky:
        async def reap_abandoned(self, ctx, older_than_seconds):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient db hiccup")
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await intake_reaper_loop(
            _Flaky(),  # type: ignore[arg-type]
            SessionContext(principal_kind="owner"),
            interval_seconds=0,
            older_than_seconds=10,
        )
    assert calls == 2  # the first error didn't kill the loop
