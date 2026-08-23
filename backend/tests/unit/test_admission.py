"""The ledger's arithmetic: admit against declarations, never against a measurement alone."""

from __future__ import annotations

import pytest

from jbrain.llm.admission import (
    Layer,
    Outcome,
    Phase,
    Pool,
    Reservation,
    admit,
    charged_gb,
)


def _res(
    served: str,
    host: float,
    device: float,
    phase: Phase = Phase.RESIDENT,
    instance: str = "",
) -> Reservation:
    return Reservation(
        instance_id=instance or f"{served}-1",
        served_model=served,
        phase=phase,
        host_gb=host,
        device_gb=device,
    )


# --- the charge -------------------------------------------------------------


@pytest.mark.parametrize("phase", list(Phase))
def test_every_phase_is_charged_in_full(phase: Phase) -> None:
    """No phase is discounted, and DRAINING is the one that matters.

    Discharging on the shutdown DECISION rather than on confirmed death is the anti-pattern
    every prior-art scheduler warns about — the kernel has freed nothing at the moment the
    intent is recorded, so a competitor admitted against the discounted figure loads into
    memory that is still occupied. On this box that is a hard freeze and a power cycle."""
    rows = [_res("gpt-oss-120b", 68.0, 68.0, phase)]
    assert charged_gb(rows, Layer.HOST) == 68.0
    assert charged_gb(rows, Layer.DEVICE) == 68.0


def test_a_restart_charges_both_instances_while_they_overlap() -> None:
    """A config change is TWO reservations, never one row resized. The old instance drains at
    its OLD declared size — we no longer have the config that produced it — and the new one is
    planned at its new size. Sum them and the answer is right whether they overlap or not."""
    rows = [
        _res("qwen3-coder-next", 49.6, 49.6, Phase.DRAINING, instance="old"),
        _res("qwen3-coder-next", 59.6, 59.6, Phase.PLANNED, instance="new"),
    ]
    assert charged_gb(rows, Layer.HOST) == pytest.approx(109.2)


# --- admission --------------------------------------------------------------


def _pool(total: float, reserve: float = 6.0, measured: float | None = None) -> Pool:
    return Pool(total_gb=total, reserve_gb=reserve, measured_free_gb=measured)


def test_an_empty_box_admits_a_model_that_fits() -> None:
    d = admit(_res("qwen3.5-4b", 4.6, 4.6), [], host=_pool(121.0), device=_pool(124.0))
    assert d.outcome is Outcome.ADMIT
    assert d.admitted and d.layer is None


def test_a_model_larger_than_the_box_is_INFEASIBLE_not_deferred() -> None:
    """The two refusals are different instructions to the caller. A worker that retries an
    infeasible load retries it forever, burning a retry budget on arithmetic that cannot
    change; a worker that gives up on a deferrable one drops work the box could have done two
    minutes later. Conflating them is what makes a conservative gate intolerable."""
    d = admit(_res("enormous", 200.0, 200.0), [], host=_pool(121.0), device=_pool(124.0))
    assert d.outcome is Outcome.INFEASIBLE
    assert "however long you wait" in d.reason


def test_a_model_that_fits_an_empty_box_but_not_this_one_is_DEFERRED() -> None:
    d = admit(
        _res("gpt-oss-120b", 68.0, 68.0),
        [_res("qwen3-coder-next", 59.6, 59.6)],
        host=_pool(121.0),
        device=_pool(124.0),
    )
    assert d.outcome is Outcome.DEFERRED
    assert "Waiting for room" in d.reason


def test_the_reserve_is_held_out_of_capacity_not_spent_on_the_last_model() -> None:
    """A model that fits the raw total and not the total-minus-reserve is refused. The reserve
    is what the host needs to stay ALIVE — the freeze mode here is a reclaim livelock, not a
    clean OOM kill, so the machine stops answering rather than losing a process."""
    d = admit(_res("snug", 118.0, 1.0), [], host=_pool(121.0, reserve=6.0), device=_pool(124.0))
    assert d.outcome is Outcome.INFEASIBLE  # 118 > 121 - 6
    assert d.layer is Layer.HOST


def test_the_device_layer_can_refuse_what_the_host_layer_allows() -> None:
    """Both layers, independently, and the failure is reported as the layer's. A single
    combined budget is how a device-side balloon (the vision projector) hid behind comfortable
    host arithmetic in every freeze this box took."""
    d = admit(
        _res("vision-heavy", 10.0, 90.0),
        [_res("resident", 10.0, 60.0)],
        host=_pool(121.0),
        device=_pool(124.0),
    )
    assert d.outcome is Outcome.DEFERRED
    assert d.layer is Layer.DEVICE


# --- the min(), which is the load-bearing half ------------------------------


def test_a_measurement_tighter_than_the_ledger_wins() -> None:
    """The ledger is blind to consumers it did not create, and this box has five — ComfyUI
    evicts LLMs and is never evicted by them, whisper is a second llama-swap, and Kokoro holds
    a model resident with no accounting anywhere in the backend. An empty ledger on a box those
    have filled must still refuse."""
    d = admit(
        _res("gpt-oss-120b", 68.0, 68.0),
        [],  # ledger says the box is empty
        host=_pool(121.0, measured=20.0),  # the OS says otherwise
        device=_pool(124.0, measured=124.0),
    )
    assert d.outcome is Outcome.DEFERRED
    assert d.layer is Layer.HOST
    assert "by measurement" in d.reason


def test_a_ledger_tighter_than_the_measurement_wins() -> None:
    """The other direction, and the one the ledger exists for: a model was admitted seconds ago
    and is still reading its weights, so almost none of its footprint has landed in
    `/proc/meminfo` yet. The measurement says there is room. There is not."""
    d = admit(
        _res("qwen3-coder-next", 59.6, 59.6),
        [_res("gpt-oss-120b", 68.0, 68.0, Phase.STARTING)],
        host=_pool(121.0, measured=115.0),  # the load has barely begun to commit
        device=_pool(124.0, measured=124.0),
    )
    assert d.outcome is Outcome.DEFERRED
    assert "by the ledger" in d.reason


def test_an_unreadable_pool_falls_back_to_the_ledger_rather_than_refusing_everything() -> None:
    """None means "we could not read it", which must never be spelled 0.0 — that is a reading,
    and it would refuse every load on a box whose probe merely went away."""
    d = admit(
        _res("qwen3.5-4b", 4.6, 4.6),
        [],
        host=_pool(121.0, measured=None),
        device=_pool(124.0, measured=None),
    )
    assert d.outcome is Outcome.ADMIT


def test_an_unreadable_pool_still_enforces_the_ledger() -> None:
    """Falling back is not giving up: the promises still bind."""
    d = admit(
        _res("gpt-oss-120b", 68.0, 68.0),
        [_res("qwen3-coder-next", 59.6, 59.6)],
        host=_pool(121.0, measured=None),
        device=_pool(124.0, measured=None),
    )
    assert d.outcome is Outcome.DEFERRED
    assert "nothing measured" in d.reason


def test_the_refusal_says_what_the_model_needs_and_what_the_box_has_separately() -> None:
    """The refusal this replaces printed the whole box's projected total as though it were one
    model's need — "gpt-oss-120b needs ~137 GB" on a 121 GB box. A refusal that misstates the
    cause is worse than a silent one, because the operator acts on it."""
    d = admit(
        _res("gpt-oss-120b", 68.0, 68.0),
        [_res("qwen3-coder-next", 59.6, 59.6)],
        host=_pool(121.0, measured=50.0),
        device=_pool(124.0),
    )
    assert "needs 68.0 GB" in d.reason
    assert "137" not in d.reason


def test_the_reserve_is_held_out_of_the_MEASURED_estimate_too() -> None:
    """Both estimates mean the same thing — room for a new model BEYOND the reserve — so the
    reserve comes off both. Applying it only to the ledger term is how a box with an empty
    ledger and a full-looking `MemFree` admits a model into exactly the pages the kernel needs
    to reclaim, which is the 2026-08-19 livelock."""
    d = admit(
        _res("snug", 20.0, 1.0),
        [],
        host=_pool(121.0, reserve=6.0, measured=20.0),  # free == need, to the byte
        device=_pool(124.0),
    )
    assert d.outcome is Outcome.DEFERRED
    assert d.layer is Layer.HOST


# --- the housekeeping rules -------------------------------------------------


def test_a_resident_reservation_never_expires() -> None:
    """The load-bearing half of the TTL. A model can legitimately serve for weeks, so expiring
    a resident row would discharge memory the box is still holding — the ledger telling the
    exact lie it exists to prevent, and worse than having no ledger, because the number would be
    confidently wrong."""
    from datetime import timedelta

    from jbrain.llm.admission import is_abandoned

    assert not is_abandoned(Phase.RESIDENT, timedelta(days=30))


def test_a_transitional_reservation_expires_only_after_its_own_ceiling() -> None:
    """Per phase, because the phases take wildly different times. STARTING's ceiling is sized
    off a MEASURED 198 s cold load of gpt-oss-120b and is deliberately generous: a load swept
    out from under itself would let a second one in beside it, which is the co-load this whole
    design exists to stop."""
    from datetime import timedelta

    from jbrain.llm.admission import is_abandoned

    assert not is_abandoned(Phase.STARTING, timedelta(seconds=198))
    assert is_abandoned(Phase.STARTING, timedelta(hours=1))
    # PLANNED is not "moments": between the charge and the spawn sit an eviction, a bounded
    # 60 s wait for a stop to settle, and possibly a config regeneration. Expiring one under a
    # live load is the worst case this table has — the load carries on and the ledger forgets
    # it — so the ceiling must clear the slowest of those steps by a wide margin.
    assert not is_abandoned(Phase.PLANNED, timedelta(seconds=90))
    assert is_abandoned(Phase.PLANNED, timedelta(hours=1))


def test_reconcile_drops_phantoms_and_only_REPORTS_what_it_did_not_admit() -> None:
    """Two failures, and only one of them is the ledger's to fix.

    A phantom — a row whose model is not running — is a charge nobody will ever release; left
    alone it shrinks the budget permanently and the box slowly refuses everything.

    A foreign model is NOT charged. Inventing a declaration for a process we did not admit
    would put a number we made up into the arithmetic that protects the box, and a made-up
    number that is too small is how a gate admits the load it should have refused. It is
    reported instead, and the `min()` against the live measurement is what covers it."""
    from jbrain.llm.admission import reconcile_split

    rows = [
        _res("gpt-oss-120b", 68.0, 68.0, instance="alive"),
        _res("qwen3-coder-next", 59.6, 59.6, instance="ghost"),
    ]
    phantoms, foreign = reconcile_split(rows, {"gpt-oss-120b", "comfyui-sdxl"})
    assert phantoms == ["ghost"]
    assert foreign == ["comfyui-sdxl"]


def test_INFEASIBLE_on_either_layer_beats_DEFERRED_on_the_other() -> None:
    """Both layers are evaluated before anything is returned, and the severer answer wins.

    Returning the first failing layer would report "retry later" for a request that is
    infeasible on the other one — and the caller then retries it forever, which is exactly the
    cost the two-outcome split exists to avoid. Unreachable on today's constants; pinned anyway,
    because those constants live in another module and nothing holds the relationship still."""
    d = admit(
        _res("lopsided", 40.0, 40.0),
        [_res("resident", 90.0, 90.0)],  # host is merely FULL: deferred, and evaluated first
        host=_pool(121.0),
        device=_pool(30.0),  # device could never hold 40 GB on an empty box: infeasible
    )
    assert d.outcome is Outcome.INFEASIBLE, "the first failing layer answered for both"
    assert d.layer is Layer.DEVICE


def test_reconcile_split_gives_the_same_answer_for_a_generator() -> None:
    """It reads `rows` twice. Handed a generator, the second pass sees nothing, `charged` comes
    back empty, and EVERY resident model is reported as foreign — a public function silently
    changing its answer with the caller's container type."""
    from jbrain.llm.admission import reconcile_split

    rows = [_res("gpt-oss-120b", 68.0, 68.0, instance="alive")]
    as_list = reconcile_split(rows, {"gpt-oss-120b"})
    as_generator = reconcile_split((r for r in rows), {"gpt-oss-120b"})
    assert as_generator == as_list == ([], [])
