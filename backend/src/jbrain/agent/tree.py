"""Per-root-turn sub-agent tree state (docs/archive/SUBAGENT_SPAWNING_PLAN.md).

The mutable state shared across one root turn's whole sub-agent fan. In Wave S1 it
carries the running agent count so the tree-size cap holds across nested fans (a
depth-1 child's own fan decrements the same counter the root started). Wave S2
extends it with the shared token budget (the incremental-spend pool + root reserve +
admission floor). It lives in its own module — importing nothing from the loop or the
spawn service — so both can reference it without an import cycle.
"""

import time
from dataclasses import dataclass

# Structural fan caps (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S1). These shape caps
# bound the tree on their own — no model cooperation.
# spawn allowed iff parent.depth < MAX_DEPTH. At 1, only the root (jerv, depth 0) may
# spawn — a child (depth 1) is always a leaf and cannot spawn further. Child-initiated
# nesting was removed (the model wouldn't use it reliably and it carried the depth>=1
# brief-laundering surface); `waves` covers structured orchestrator-declared work.
MAX_DEPTH = 1
MAX_CHILDREN_PER_PARENT = 6  # the largest fan a single spawn call may launch
MAX_PARALLEL = 4  # the most children that run concurrently within a fan
MAX_TOTAL_AGENTS_PER_TREE = 12  # every child across the whole root turn, all depths
# Feeding waves (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md): a single staged spawn call may
# chain at most this many ordered waves (a producer wave → a consumer wave). Kept at 2
# so the serial local wall-clock stays under the parent turn cap and the surface stays
# legible; the total children across all waves still obey MAX_CHILDREN_PER_PARENT.
MAX_WAVES = 2

# Deep research (docs/plans/DEEP_RESEARCH_TOOL_PLAN.md) bounds its gather rounds the same
# way — one gather fan and at most one gap-refill — but STRUCTURALLY (there is no loop in
# `deep_research.py` that could run a third round), so no constant governs it; it is a
# bounded pipeline, not an adaptive "research until covered" one.

# Per-child RUNTIME bounds. The original tight caps (10 steps / 180s) were sized for a
# PARALLEL fan on a slow single-GPU box; now the fan runs SERIALLY on a local route
# (each child gets the whole device), so a child can afford to actually research —
# search, read several sources, and synthesize — before a cap bites. The step cap is
# the primary bound and SCALES WITH EFFORT (a "high" research child needs many more
# ReAct turns than a quick lookup); the wall-clock is a generous backstop kept above
# the step budget's expected runtime so a child reaches its step cap (→ a forced final
# answer) rather than a bare timeout; the token cap is the last backstop.
CHILD_MAX_STEPS = 24  # default/low-effort ReAct iterations a child may take
# Effort lifts the step cap: a high-effort child gets the room to do thorough research.
# The runaway risk was a RETRY LOOP (jerv re-spawning fans), now closed by the prompt —
# so a single fan can afford generous per-child budgets without pegging the box forever.
# (Doubled from the original 12/22/32 so a research child rarely truncates mid-chain; on a
# slow local box the per-child wall-clock below becomes the practical binding limit.)
CHILD_STEPS_BY_EFFORT = {"high": 64, "medium": 44}
CHILD_WALL_CLOCK_S = 1200.0  # hard per-child time limit; past it the child returns truncated
# (Doubled from 600s with the step caps so a medium/high child can actually reach its
# larger step budget on the slow local box before the clock — not the steps — stops it.)
CHILD_MAX_COST_TOKENS = 900_000  # per-child token backstop (steps/wall-clock bite first)


def child_steps_for(effort: str | None) -> int:
    """The ReAct step cap for a child of the given reasoning effort — higher effort
    earns a longer chain (more searches/reads) before the cap stops it."""
    return CHILD_STEPS_BY_EFFORT.get(effort or "", CHILD_MAX_STEPS)


# Token-budget shape (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S2). The tree may spend at
# most SPAWN_MULTIPLIER × the root's own per-turn token cap; a fraction is reserved off
# the top so the root can always synthesize even after a fan drains the children's
# pool; and a fan is admitted only if each child could get a viable slice of what's
# left. Sized generously (~6M children pool with jerv's 800k root cap) so the runtime
# caps above — not budget exhaustion — are what stop a child, with ample room for a staged
# review reserve (deep_research) on top of a full multi-round gather.
SPAWN_MULTIPLIER = 10.0  # tree_budget = base_max_cost_tokens × this (~8.0M for jerv)
ROOT_RESERVE_FRACTION = 0.25  # share of tree_budget the root keeps for synthesis
MIN_VIABLE_CHILD_BUDGET = 100_000  # admission floor: tokens each child must be able to get

# Feeding waves runtime bound (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md, F2). A whole staged
# (feeding) spawn call must finish inside this cumulative wall-clock, sized to sit under
# the parent turn cap (_MAX_TURN_WALL_CLOCK_S=3600s) with ~600s of synthesis headroom.
# Checked at each wave barrier; a wave that can't start before it is skipped, loud. Only
# the staged scheduler consults it — a flat fan is unchanged.
TREE_WALL_CLOCK_S = 3000.0


@dataclass
class TreeState:
    """Mutable state for one root turn's sub-agent tree.

    `agents_spawned` counts every child minted so far across the whole tree (all
    depths, every fan), so the total-agents cap holds even when more than one fan
    runs. `spent` is the running **incremental** token spend across the tree — every
    model call in any loop (root or child) charges it via `charge`. The budget is a
    single shared pool (the true ceiling); `root_reserve` is carved off the top so a
    fan leaves the root room to synthesize, and the admission floor keeps a fan from
    launching children too small to be useful.

    **Concurrency note (the reserve is best-effort, the total is hard).** Loops charge
    after each model call and check exhaustion after charging, so a fan running up to
    `max_parallel` children can have that many calls already in flight when the
    children's pool is crossed — `spent` overshoots `tree_budget - root_reserve` by at
    most that bounded batch, eroding (not breaching) the reserve. What is HARD: total
    tree spend is bounded (each loop stops at the first post-call check past its
    ceiling, so the worst case is `tree_budget` + one bounded in-flight batch — no
    runaway), and the root ALWAYS completes at least its synthesis call (the budget
    check is post-call, so the root makes its final `converse` before stopping). The
    reserve simply buys that synthesis comfortable multi-call room in the common case.
    Sizing pre-reservation to make the reserve a hard floor was judged not worth the
    complexity for a cost-bounded comfort cushion."""

    agents_spawned: int = 0
    max_total_agents: int = MAX_TOTAL_AGENTS_PER_TREE
    # 0 means "budget not seeded" (a non-spawn turn that still passes a TreeState):
    # charge/exhaustion are no-ops, so an ordinary turn is governed only by its own
    # per-loop Guardrails, exactly as before Wave S2.
    tree_budget: int = 0
    root_reserve: int = 0
    spent: int = 0
    # A monotonic deadline for a whole staged (feeding) call, or None when unbounded
    # (a flat fan never consults it). `rooted` stamps it TREE_WALL_CLOCK_S out.
    deadline: float | None = None
    # A reserve carved for a not-yet-run FINAL wave of a staged call (F2): set to
    # `len(final_wave) × MIN_VIABLE_CHILD_BUDGET` while earlier waves run so an
    # over-spending producer cannot starve the deliverable wave, then released to 0
    # when the final wave itself starts. 0 for a flat fan (no effect).
    stage_reserve: int = 0

    @classmethod
    def rooted(cls, base_max_cost_tokens: int) -> "TreeState":
        """The tree state for a root turn, with the budget sized off the root's own
        per-turn cap (the locked spawn multiplier), the root reserve carved off, and the
        staged-call wall-clock deadline stamped (a flat fan ignores it)."""
        tree_budget = int(base_max_cost_tokens * SPAWN_MULTIPLIER)
        return cls(
            tree_budget=tree_budget,
            root_reserve=int(tree_budget * ROOT_RESERVE_FRACTION),
            deadline=time.monotonic() + TREE_WALL_CLOCK_S,
        )

    def out_of_time(self) -> bool:
        """True once a staged call's cumulative wall-clock deadline has passed — the
        structural bound the per-child clock never provided. None deadline → never."""
        return self.deadline is not None and time.monotonic() >= self.deadline

    def seconds_left(self) -> float | None:
        """Wall-clock remaining before the staged deadline, or None if unbounded."""
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def can_admit(self, n: int) -> bool:
        """Whether this fan of `n` children fits under the tree-wide total cap."""
        return self.agents_spawned + n <= self.max_total_agents

    def admit(self, n: int) -> None:
        """Reserve `n` child slots against the tree total (called once a fan clears
        admission, before its children launch)."""
        self.agents_spawned += n

    # --- token budget (Wave S2) ---------------------------------------------
    def charge(self, tokens: int) -> None:
        """Charge a model call's incremental spend to the shared pool."""
        self.spent += tokens

    def children_budget(self) -> int:
        """The sub-agent budget CEILING — the children's pool (tree_budget minus the root's
        reserve), i.e. where a child actually stops (`children_exhausted`). This is what the
        budget meter shows as its denominator, so the bar fills as children exhaust instead
        of leaving the root's reserve as phantom headroom the bar can never reach. Ignores
        the transient `stage_reserve` (a within-run reservation) so the meter's ceiling is
        stable across a run."""
        return max(0, self.tree_budget - self.root_reserve)

    def children_remaining(self) -> int:
        """Tokens a fan may still draw — the pool minus the root reserve, the (staged)
        final-wave reserve, and all spend so far (root's own calls included; the pool is
        genuinely shared). `stage_reserve` is 0 for a flat fan, so its behaviour is
        unchanged."""
        if self.tree_budget <= 0:
            return 0
        return max(0, self.tree_budget - self.root_reserve - self.stage_reserve - self.spent)

    def can_admit_budget(self, n: int) -> bool:
        """The admission floor: a fan of `n` is admitted only if what's left in the
        children's pool covers a minimum viable slice for each."""
        if self.tree_budget <= 0:
            return True  # budget not seeded → only the structural caps apply
        return self.children_remaining() >= n * MIN_VIABLE_CHILD_BUDGET

    def root_exhausted(self) -> bool:
        """True once total tree spend has reached the whole pool — the root itself
        must stop (it has spent even its reserve)."""
        return self.tree_budget > 0 and self.spent >= self.tree_budget

    def children_exhausted(self) -> bool:
        """True once tree spend has reached the children's pool — a child must stop
        here, leaving the root reserve (and any `stage_reserve` carved for a not-yet-run
        review/final wave) intact. Defined as `children_remaining() == 0` so the
        spend-time stop matches the admission gate: a greedy producer fan is halted at
        the reserve, never allowed to eat the slice a later stage was promised (the
        1918-flu run: gather drained the pool and the cross-check analyst was killed
        mid-search)."""
        return self.tree_budget > 0 and self.children_remaining() == 0
