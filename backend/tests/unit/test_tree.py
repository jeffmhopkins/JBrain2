"""The sub-agent tree budget (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S2): the shared
incremental-spend pool, the root reserve, and the admission floor — all pure, no
adapter, no model cooperation."""

from jbrain.agent.tree import (
    CHILD_MAX_STEPS,
    MIN_VIABLE_CHILD_BUDGET,
    ROOT_RESERVE_FRACTION,
    SPAWN_MULTIPLIER,
    TreeState,
    child_steps_for,
)


def test_child_steps_scale_with_effort() -> None:
    """A higher-effort child earns a longer ReAct chain; unknown/absent effort falls
    back to the base cap."""
    assert child_steps_for("high") == 64
    assert child_steps_for("medium") == 44
    assert child_steps_for("low") == CHILD_MAX_STEPS
    assert child_steps_for("none") == CHILD_MAX_STEPS
    assert child_steps_for(None) == CHILD_MAX_STEPS


def test_rooted_sizes_budget_and_reserve_off_the_root_cap() -> None:
    tree = TreeState.rooted(800_000)
    assert tree.tree_budget == int(800_000 * SPAWN_MULTIPLIER)  # 4_000_000
    assert tree.root_reserve == int(tree.tree_budget * ROOT_RESERVE_FRACTION)  # 1_000_000
    # The children's pool is the budget minus the reserve.
    assert tree.children_remaining() == tree.tree_budget - tree.root_reserve  # 3_000_000


def test_charge_draws_down_the_shared_pool() -> None:
    tree = TreeState.rooted(800_000)  # children pool 3.0M
    tree.charge(250_000)
    assert tree.spent == 250_000
    assert tree.children_remaining() == 3_000_000 - 250_000


def test_admission_floor_refuses_a_fan_that_cannot_seat_each_child() -> None:
    tree = TreeState.rooted(800_000)  # children pool 3.0M
    assert tree.can_admit_budget(6)  # 6 × 100k = 600k <= 3.0M
    tree.charge(2_600_000)  # children pool now 400_000
    assert not tree.can_admit_budget(5)  # 500k > 400k
    assert tree.can_admit_budget(4)  # 400k <= 400k
    assert MIN_VIABLE_CHILD_BUDGET == 100_000  # the floor these numbers assume


def test_children_stop_at_the_pool_but_the_root_reserve_survives() -> None:
    tree = TreeState.rooted(800_000)  # budget 4.0M, reserve 1.0M, children pool 3.0M
    tree.charge(3_000_000)  # children have eaten the whole children's pool
    assert tree.children_exhausted()  # a child must stop here
    assert not tree.root_exhausted()  # ...but the root still has its 1.0M reserve
    assert tree.tree_budget - tree.spent == tree.root_reserve


def test_children_stop_early_when_a_stage_reserve_is_carved() -> None:
    """The spend-time stop honours `stage_reserve`, so a greedy producer fan is halted
    at the reserve — never allowed to eat the slice a later stage (deep_research's
    analyst/critique, or a staged final wave) was promised. Matches the admission gate:
    `children_exhausted` is exactly `children_remaining() == 0`."""
    tree = TreeState.rooted(800_000)  # children pool 3.0M
    tree.stage_reserve = 600_000  # carve a review slice off the pool
    tree.charge(2_400_000)  # producers have eaten the pool DOWN TO the reserve
    assert tree.children_remaining() == 0
    assert tree.children_exhausted()  # a producer child must stop, leaving the reserve
    assert not tree.root_exhausted()  # neither reserve has been touched
    tree.stage_reserve = 0  # the reserved stage is reached — its slice is released
    assert tree.children_remaining() == 600_000
    assert not tree.children_exhausted()  # the review child may now spend it


def test_root_exhausts_only_at_the_whole_pool() -> None:
    tree = TreeState.rooted(800_000)
    tree.charge(4_000_000)
    assert tree.root_exhausted()
    assert tree.children_exhausted()


def test_unseeded_budget_is_inert() -> None:
    """A plain TreeState (an ordinary non-spawn turn still passes one) has no seeded
    budget, so the budget machinery is a no-op — the turn is bounded only by its own
    per-loop Guardrails, exactly as before Wave S2."""
    tree = TreeState()
    assert tree.tree_budget == 0
    tree.charge(10_000_000)
    assert tree.children_remaining() == 0
    assert tree.can_admit_budget(6)  # structural caps only
    assert not tree.root_exhausted()
    assert not tree.children_exhausted()


def test_out_of_time_tracks_the_staged_deadline() -> None:
    """F2: the staged wall-clock deadline is the structural bound the per-child clock
    never provided; an unbounded (None) deadline never fires."""
    import time

    assert TreeState(deadline=time.monotonic() - 1).out_of_time()
    assert not TreeState(deadline=time.monotonic() + 100).out_of_time()
    assert not TreeState().out_of_time()  # no deadline → never


def test_children_remaining_honors_the_final_wave_reserve() -> None:
    """F2: the staged final-wave reserve is carved off the children's pool so an
    over-spending earlier wave cannot starve the deliverable wave. 0 for a flat fan."""
    tree = TreeState(tree_budget=500_000, root_reserve=100_000)
    assert tree.children_remaining() == 400_000  # no stage reserve by default
    tree.stage_reserve = 150_000
    assert tree.children_remaining() == 250_000
    assert not tree.can_admit_budget(3)  # 250k < 3 × 100k floor
    tree.stage_reserve = 0  # released to the final wave
    assert tree.children_remaining() == 400_000


def test_rooted_stamps_a_wall_clock_deadline() -> None:
    """A rooted tree carries the staged deadline (a flat fan ignores it)."""
    tree = TreeState.rooted(800_000)
    assert tree.deadline is not None
    assert not tree.out_of_time()
    assert tree.stage_reserve == 0
