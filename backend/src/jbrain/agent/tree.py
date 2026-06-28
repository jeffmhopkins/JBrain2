"""Per-root-turn sub-agent tree state (docs/SUBAGENT_SPAWNING_PLAN.md).

The mutable state shared across one root turn's whole sub-agent fan. In Wave S1 it
carries the running agent count so the tree-size cap holds across nested fans (a
depth-1 child's own fan decrements the same counter the root started). Wave S2
extends it with the shared token budget (the incremental-spend pool + root reserve +
admission floor). It lives in its own module — importing nothing from the loop or the
spawn service — so both can reference it without an import cycle.
"""

from dataclasses import dataclass

# Structural fan caps (docs/SUBAGENT_SPAWNING_PLAN.md, Wave S1). These shape caps
# bound the tree on their own — no model cooperation; the budget-derived numbers
# (per-child / tree token pool) are layered on in Wave S2.
MAX_DEPTH = 2  # spawn allowed iff parent.depth < MAX_DEPTH (root=0; depth-2 is a leaf)
MAX_CHILDREN_PER_PARENT = 6  # the largest fan a single spawn call may launch
MAX_PARALLEL = 4  # the most children that run concurrently within a fan
MAX_TOTAL_AGENTS_PER_TREE = 12  # every child across the whole root turn, all depths


@dataclass
class TreeState:
    """Mutable state for one root turn's sub-agent tree. `agents_spawned` counts
    every child minted so far across the whole tree (all depths, every fan in the
    turn), so the total-agents cap holds even when more than one fan runs."""

    agents_spawned: int = 0
    max_total_agents: int = MAX_TOTAL_AGENTS_PER_TREE

    def can_admit(self, n: int) -> bool:
        """Whether this fan of `n` children fits under the tree-wide total cap."""
        return self.agents_spawned + n <= self.max_total_agents

    def admit(self, n: int) -> None:
        """Reserve `n` child slots against the tree total (called once a fan clears
        admission, before its children launch)."""
        self.agents_spawned += n
