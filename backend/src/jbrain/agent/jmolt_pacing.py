"""What jmolt experiences when it writes, independent of where the bytes go.

The switch changes the DESTINATION of a write, never the rhythm. Autonomy on sends it to
Moltbook now; autonomy off stages it for the drip. Either way jmolt is held to the same
per-minute budget and the same gap between writes, and is told the same two things: how much
room is left, and — when there is none — exactly how long until there is.

That symmetry is the point. With the switch off the old handlers enforced no rate at all, so
jmolt learned nothing about pacing in the safe mode and would have met the real limits for the
first time on the night they started having consequences.

Two ledgers, deliberately, because one cannot do both jobs:

- **This one paces the AGENT**, and is charged when jmolt calls a write tool.
- **`web.moltbook.RateLedger` inside the client protects the PLATFORM**, and is charged on the
  real HTTP call.

Under autonomy they fire at the same instant and agree. With it off they must not be the same
ledger: a row staged at 03:12 and published at 14:30 falls in two different minutes, and a
sliding 60-second window cannot be reserved eleven hours ahead.

The gap is a separate check from the budget on purpose. A sliding-window count is not a rate —
25 writes in one second satisfy it exactly as well as 25 across a minute — which is how twelve
writes went out inside three seconds on 2026-08-26 and the platform killed seven of them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jbrain.web.moltbook import RateLedger

# Matches the drip's own spacing (`jmolt_sweep.JMOLT_WRITE_GAP_S`) so the rhythm jmolt learns
# is the rhythm its writes actually go out at.
WRITE_GAP_S = 3.0


@dataclass
class WritePacer:
    """jmolt's own write budget: a per-minute window plus a minimum gap between writes."""

    ledger: RateLedger = field(default_factory=RateLedger)
    clock: Callable[[], float] = time.monotonic
    gap_s: float = WRITE_GAP_S
    _last_write: float | None = None

    def refusal(self) -> str | None:
        """None when a write may go now; otherwise the agent-facing reason, naming the wait.

        Deliberately a refusal rather than a sleep. A hidden `sleep` inside every write would
        quietly spend a minute of a sixty-minute night and teach jmolt nothing; being told to
        come back in three seconds is information it can act on."""
        if self._last_write is not None:
            waited = self.clock() - self._last_write
            if waited < self.gap_s:
                return (
                    f"Too soon — you wrote {waited:.0f}s ago and writes go out at least "
                    f"{self.gap_s:.0f}s apart. Try again in {self._ceil(self.gap_s - waited)}s; "
                    "read something in the meantime."
                )
        after = self.ledger.retry_after("write")
        if after > 0:
            return (
                f"You have used your writes for this minute ({self.ledger.write_per_min}). "
                f"Try again in {self._ceil(after)}s — the limit is a rolling minute, so it "
                "frees up gradually rather than all at once."
            )
        return None

    def charge(self) -> None:
        """Spend one write. Called only once a write has actually gone through, so a refused
        or guard-blocked write never burns budget jmolt did not use."""
        self.ledger.charge("write")
        self._last_write = self.clock()

    def headroom(self) -> str:
        """The one-line budget report appended to a successful write."""
        left = self.ledger.remaining("write")
        return f"{left} more write{'' if left == 1 else 's'} available this minute."

    @staticmethod
    def _ceil(seconds: float) -> int:
        """Round UP, and never to zero: telling jmolt to retry in 0s invites an immediate
        retry that is refused again."""
        return max(1, int(seconds) + (1 if seconds % 1 else 0))
