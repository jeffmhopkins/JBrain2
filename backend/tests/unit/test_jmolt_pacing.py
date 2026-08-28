"""jmolt's write pacing — identical whether or not the autonomy switch is on.

The switch changes where a write GOES, never the rhythm. With it off the handlers used to
enforce no rate at all, so jmolt learned nothing about pacing in the safe mode and would have
met the real limits for the first time on the night they started having consequences.

The gap is a separate check from the per-minute budget on purpose. A sliding-window count is
not a rate — 25 writes in one second satisfy it exactly as well as 25 across a minute — which
is how twelve writes went out inside three seconds on 2026-08-26 and the platform killed seven.
"""

from __future__ import annotations

from jbrain.agent.jmolt_pacing import WritePacer
from jbrain.web.moltbook import RateLedger


def _pacer(now: list[float]) -> WritePacer:
    clock = lambda: now[0]  # noqa: E731 — a settable fake clock, one line
    return WritePacer(ledger=RateLedger(clock=clock), clock=clock)


def test_the_first_write_is_allowed() -> None:
    assert _pacer([100.0]).refusal() is None


def test_a_second_write_too_soon_is_refused_with_the_wait() -> None:
    now = [100.0]
    p = _pacer(now)
    p.charge()
    now[0] = 101.0
    refusal = p.refusal()
    assert refusal is not None
    assert "Try again in 2s" in refusal


def test_the_gap_clears_on_its_own() -> None:
    now = [100.0]
    p = _pacer(now)
    p.charge()
    now[0] = 103.0
    assert p.refusal() is None


def test_the_wait_never_rounds_down_to_zero() -> None:
    """ "Try again in 0s" invites an immediate retry that is refused again."""
    now = [100.0]
    p = _pacer(now)
    p.charge()
    now[0] = 102.99
    refusal = p.refusal()
    assert refusal is not None and "in 1s" in refusal


def test_the_gap_alone_holds_throughput_under_the_per_minute_budget() -> None:
    """Worth stating: at a 3s gap the ceiling is 20 writes/min, under the 25 budget. So the
    GAP is the control that actually binds and the count is a backstop — which is the right
    way round, since the count provably cannot stop a burst."""
    now = [100.0]
    p = _pacer(now)
    sent = 0
    for _ in range(600):  # a minute of tenth-second attempts
        if p.refusal() is None:
            p.charge()
            sent += 1
        now[0] += 0.1
    assert sent == 20
    assert sent < p.ledger.write_per_min


def test_the_per_minute_budget_refuses_with_the_time_the_window_frees() -> None:
    """The window is sliding, so the answer is when the OLDEST call ages out — not a flat 60s.
    Charged through the ledger directly, because the gap would otherwise bind first."""
    now = [100.0]
    p = _pacer(now)
    for _ in range(p.ledger.write_per_min):
        p.ledger.charge("write")
    now[0] = 145.0  # 45s after the window filled; the oldest ages out at t=160
    refusal = p.refusal()
    assert refusal is not None
    assert "used your writes for this minute" in refusal
    assert "Try again in 15s" in refusal


def test_a_burst_is_stopped_by_the_gap_not_the_count() -> None:
    """The live failure: twelve writes inside three seconds. The per-minute budget was never
    exceeded and would not have stopped it; only the gap does."""
    now = [100.0]
    p = _pacer(now)
    p.charge()
    allowed = 0
    for _ in range(11):
        now[0] += 0.25
        if p.refusal() is None:
            allowed += 1
            p.charge()
    assert allowed == 0, "a sub-second burst must be refused every time"
    assert p.ledger.remaining("write") == 24  # the count alone saw nothing wrong


def test_headroom_is_reported_and_counts_down() -> None:
    now = [100.0]
    p = _pacer(now)
    assert "25 more writes" in p.headroom()
    p.charge()
    assert "24 more writes" in p.headroom()


def test_headroom_is_singular_at_one() -> None:
    now = [100.0]
    p = _pacer(now)
    for _ in range(p.ledger.write_per_min - 1):
        p.ledger.charge("write")
    assert "1 more write available" in p.headroom()


def test_a_refused_write_does_not_spend_budget() -> None:
    """`charge` is called only once a write has gone through, so a guard-blocked or refused
    write never burns budget jmolt did not use."""
    now = [100.0]
    p = _pacer(now)
    p.charge()
    before = p.ledger.remaining("write")
    now[0] = 100.5
    assert p.refusal() is not None
    assert p.ledger.remaining("write") == before
