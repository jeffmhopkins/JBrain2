"""When jmolt can publish, and when the tools simply are not there.

Restraint by structure (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2). The fourth thing all six
cold designers said independently: restraint comes from structure, and its strongest form is
making publishing tools ABSENT from most of the hour rather than discouraging their use in
prose.

The distinction is not stylistic. Every "post sparingly" line in a system prompt is a soft
constraint on a 120B — the threat model says so in as many words — and the measured behaviour
agrees: the night that produced seventeen comments on one post ran under a prompt that asked
for restraint. A tool that is not in the schema cannot be called by a model that has decided
it should be.

Three phases, in order, over the night's sitting budget:

- **reading** — no publishing tools at all. Most of the hour. This is where the agent is
  supposed to find out what is happening, and it is the phase the current engine effectively
  never has, because its first sitting can post and so its first sitting does.
- **writing** — the publishing tools exist. A single bounded window, not a permission that
  opens and stays open, so "I have not posted yet" cannot become pressure that builds all
  night.
- **tending** — no publishing tools again. The night's last sitting, for closing what was
  finished and abandoning what was not.

The shape is a property of the SITTING INDEX, not the clock, because sittings are what the
agent experiences: a slow night and a fast one should feel the same from inside.
"""

from __future__ import annotations

from typing import Literal

Phase = Literal["reading", "writing", "tending"]

# The tools that only exist in the writing window. `moltbook_vote` and `moltbook_social` are
# NOT here: a vote is not a claim, and following someone is how an interest becomes durable —
# throttling those would suppress the behaviour the engine is trying to grow, not the one it is
# trying to stop. `moltbook_profile_update` is excluded for the same reason it is rare: it says
# nothing to anyone.
PUBLISHING_TOOLS = frozenset({"moltbook_post", "moltbook_comment"})

# Of a night's sittings, the fraction spent reading before the window opens. Two thirds, so
# "most of the hour" is literal rather than aspirational.
READ_SHARE = 2 / 3


def phase_for(sitting: int, *, budget: int) -> Phase:
    """Which phase sitting `sitting` (1-based) is in, out of `budget` sittings.

    The last sitting is always `tending`, and it is taken out of the budget before the split —
    so a short night loses writing time rather than losing the sitting where it tidies up.
    That ordering matters: an agent that never closes an obligation accumulates a brief it
    cannot finish reading, which is the same pressure as never having posted.
    """
    if budget <= 1:
        # One sitting is a night with no room to structure. It reads; it does not publish on
        # an impulse it had no time to check.
        return "reading" if sitting <= 1 else "tending"
    if sitting >= budget:
        return "tending"
    # At least one reading sitting always, and at least one writing sitting whenever the
    # budget affords one — a night that could never publish is a different experiment.
    read_sittings = max(1, min(budget - 2, round((budget - 1) * READ_SHARE)))
    return "reading" if sitting <= read_sittings else "writing"


def hidden_for(phase: Phase) -> frozenset[str]:
    """The tools removed from the turn's schema in this phase. Fails CLOSED: an unrecognised
    phase hides the publishing tools rather than exposing them, because the failure that costs
    something here is the one that hands the agent a tool it was meant to be without."""
    return frozenset() if phase == "writing" else PUBLISHING_TOOLS
