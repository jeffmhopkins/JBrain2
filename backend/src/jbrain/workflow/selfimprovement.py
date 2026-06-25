"""The shared spend-gate decision type (docs/WORKFLOW_ENGINE_PLAN.md E5, I-10).

`BudgetDecision` is the fail-closed verdict returned by a spend gate: whether an
action may run, the headroom that informed it, and a human-readable reason. The
wiki-build gate (`jbrain.wiki.budget`) reuses it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetDecision:
    """Whether a budgeted action may run, plus the headroom that informed it.
    `allowed` is False whenever the kill-switch is on or the estimated cost would
    overrun the day's remaining budget."""

    allowed: bool
    remaining: int
    reason: str
