"""The reservation ledger's arithmetic, with no I/O in it.

WHY THIS IS A SEPARATE MODULE. Every previous attempt to stop this box loading past its
memory failed the same way: a gate READ MEMORY and compared it to a prediction, so a model in
transition was counted by one layer, corrected in another, and double-counted where the two
met. Three attempts, three double-counts. See `docs/plans/LOCAL_MODEL_LEDGER_PLAN.md`.

The answer is not a better measurement. It is to admit against DECLARATIONS — one row per
model INSTANCE, both size figures declared at intent and immutable for that instance's life —
and to keep the arithmetic that decides admission somewhere it can be read in one sitting and
tested without a database, a GPU or a box. This module is that arithmetic. It knows nothing
about Postgres, llama-swap or `/proc/meminfo`; callers hand it rows and readings.

Prior art, all of it pointing the same way: Kubernetes admits against *requests*, never live
usage. Borg dares use measured usage only for a tier it is willing to kill, which this box does
not have — every model here is `prod` and the failure mode is a host lock-up, not a rescheduled
pod. Linux's `Committed_AS`/`CommitLimit` refuses an allocation that exceeds *promises* even
with RAM free. Ollama's own source calls CUDA's free-memory reporting "laggy"."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

# The box-wide advisory-lock key, shared by the evict+load path (`residency`) and the ledger's
# admit-and-charge. It lives HERE, in the module with no intra-package imports, because both of
# those import this one — and two spellings of this constant would make the two paths serialize
# against nothing at all, which is the failure that looks exactly like the lock working.
BOX_LOCK_KEY = 0x6A_42_52_41_4E_4C_4F_41  # "jBRANLOA"


class Phase(enum.StrEnum):
    """A reservation's life. The row is deleted on confirmed death; there is no terminal phase.

    `DRAINING` KEEPS THE FULL CHARGE. Discharging on the shutdown *decision* rather than on
    confirmed death is the single anti-pattern every prior-art system warns about — it is the
    one that OOMs the box, because the kernel has not freed anything at the moment the intent
    is recorded. The charge is released when the process is reaped."""

    PLANNED = "planned"  # charged, nothing spawned yet
    STARTING = "starting"  # the process exists and is reading weights
    RESIDENT = "resident"  # up and serving
    DRAINING = "draining"  # asked to stop; STILL CHARGED IN FULL


@dataclass(frozen=True)
class Reservation:
    """One model instance's declared cost, in both pools.

    Both figures are DECLARED, never measured, and IMMUTABLE for the instance's lifetime — the
    arithmetic that admitted a model is the arithmetic that protects it. A restart with a
    changed config is therefore TWO reservations, the old one draining at its old size and the
    new one planned at its new size, never one row mutated in place. That yields `max(old,
    new)` when the teardown completes first and a correct `old + new` if they ever overlap.

    Device is a COLUMN HERE, not a second ledger. That is the whole point: double-counting
    across the host and device layers becomes unrepresentable rather than something each layer
    has to remember to correct. (It also means a future vLLM-style sleep, which moves weights
    off the device while the server lives, is bytes moving between two columns of one row.)"""

    instance_id: str
    served_model: str
    phase: Phase
    host_gb: float
    device_gb: float


class Layer(enum.StrEnum):
    """The two pools admission is tested against, independently, both of which must pass."""

    HOST = "host"
    DEVICE = "device"


@dataclass(frozen=True)
class Pool:
    """One layer's fixed capacity, the reserve held out of it, and what was last measured.

    `measured_free_gb` is None for "we could not read it" — never 0.0, which is a reading that
    would refuse everything. A layer with no reading falls back to the ledger term alone, which
    is the ledger's own answer and is what a box with no probe gets."""

    total_gb: float
    reserve_gb: float
    measured_free_gb: float | None


class Outcome(enum.StrEnum):
    """Refusals are split because the CALLER'S CORRECT RESPONSE DIFFERS, and conflating them is
    what makes a conservative gate intolerable: today's refusal burns a worker retry on a
    condition that will clear by itself in two minutes."""

    ADMIT = "admit"
    DEFERRED = "deferred"  # fits this box, not right now — retry
    INFEASIBLE = "infeasible"  # exceeds total capacity — never retry


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    layer: Layer | None  # which pool bound the decision; None when admitted

    @property
    def admitted(self) -> bool:
        return self.outcome is Outcome.ADMIT


def charged_gb(rows: Iterable[Reservation], layer: Layer) -> float:
    """What the ledger has promised away in one pool.

    EVERY row counts, in every phase. There is no phase whose charge is partial: `PLANNED` has
    not allocated yet but is about to, and `DRAINING` has been asked to stop but has not
    released. Both are counted in full, and the reason is the same in both directions — the
    ledger's job is to be right about what the box is ABOUT TO be holding, not about what it is
    holding at the instant of the query. A phase-weighted charge would reintroduce exactly the
    transition blindness this exists to remove."""
    return sum(r.host_gb if layer is Layer.HOST else r.device_gb for r in rows)


def _available_gb(pool: Pool, charged: float) -> tuple[float, str]:
    """Room for a NEW instance in one pool, and a sentence saying how it was derived.

    Two estimates, and the SMALLER wins — not belt-and-braces, but because THE TWO FAIL IN
    OPPOSITE DIRECTIONS. The ledger is blind to consumers it did not create, and this box has
    five: ComfyUI evicts LLMs and is never evicted by them, whisper is a second llama-swap, and
    Kokoro holds a model resident with no accounting anywhere in `backend/src`. The measurement
    is blind to transitions — the thing the ledger exists for. `min` is the only combination
    whose error always points at "refuse"."""
    from_ledger = pool.total_gb - pool.reserve_gb - charged
    if pool.measured_free_gb is None:
        return from_ledger, (
            f"{from_ledger:.1f} GB free by the ledger ({pool.total_gb:.1f} total, "
            f"{pool.reserve_gb:.1f} reserved, {charged:.1f} promised) — nothing measured"
        )
    from_measurement = pool.measured_free_gb - pool.reserve_gb
    if from_ledger <= from_measurement:
        return from_ledger, (
            f"{from_ledger:.1f} GB free by the ledger ({pool.total_gb:.1f} total, "
            f"{pool.reserve_gb:.1f} reserved, {charged:.1f} promised); "
            f"{from_measurement:.1f} GB by measurement"
        )
    return from_measurement, (
        f"{from_measurement:.1f} GB free by measurement ({pool.measured_free_gb:.1f} free, "
        f"{pool.reserve_gb:.1f} reserved); {from_ledger:.1f} GB by the ledger"
    )


def admit(
    request: Reservation,
    rows: Iterable[Reservation],
    *,
    host: Pool,
    device: Pool,
) -> Decision:
    """Decide whether one new instance may be charged. BOTH layers must pass.

    `rows` is the whole ledger, including any row for this same model — a reload is two
    instances and the overlap is real. It must NOT include `request` itself.

    A request larger than a layer's whole usable capacity is `INFEASIBLE` and says so, because
    a caller that retries it will retry it forever. Everything else that does not fit is
    `DEFERRED`: the box could hold it once something leaves."""
    rows = list(rows)
    # BOTH layers are evaluated before anything is returned, and INFEASIBLE beats DEFERRED.
    # Returning the first failing layer would report "retry later" for a request that is
    # infeasible on the OTHER one — and the caller then retries it forever, which is the exact
    # cost this split exists to avoid. Unreachable on today's constants (the host reserve is
    # the larger, so host binds first), and pinned anyway: the constants live in another module
    # and nothing holds that relationship still.
    refusals: list[Decision] = []
    for layer, pool in ((Layer.HOST, host), (Layer.DEVICE, device)):
        need = request.host_gb if layer is Layer.HOST else request.device_gb
        usable = pool.total_gb - pool.reserve_gb
        if need > usable:
            refusals.append(
                Decision(
                    Outcome.INFEASIBLE,
                    f"{request.served_model} needs {need:.1f} GB of {layer} memory and this "
                    f"box has {usable:.1f} GB to give ({pool.total_gb:.1f} total, "
                    f"{pool.reserve_gb:.1f} held in reserve) — it will not fit however long "
                    "you wait.",
                    layer,
                )
            )
            continue
        available, how = _available_gb(pool, charged_gb(rows, layer))
        if need > available:
            refusals.append(
                Decision(
                    Outcome.DEFERRED,
                    f"{request.served_model} needs {need:.1f} GB of {layer} memory and there "
                    f"is {how}. Waiting for room rather than risking the box.",
                    layer,
                )
            )
    for refusal in refusals:
        if refusal.outcome is Outcome.INFEASIBLE:
            return refusal
    if refusals:
        return refusals[0]
    return Decision(Outcome.ADMIT, f"{request.served_model} fits in both pools.", None)


# --- the two housekeeping rules, kept here because they are decisions, not queries ---------

# How long a reservation may sit in one phase before a sweep treats it as abandoned. A
# BACKSTOP, not the mechanism: every failure path rolls its own charge back explicitly, so a
# row that fires this is a bug report. The figures differ by phase because the phases do:
# PLANNED is charged-but-not-spawned, and it is NOT "moments" as a first version of this said:
# between the charge and the spawn sit the eviction of one or more models, a wait for a stop to
# settle (bounded at 60 s), and possibly a llama-swap config regeneration. Two minutes was
# thinner than one of those steps, and expiring a PLANNED row under a live load is the worst
# case this table has — the load carries on and the ledger forgets it. STARTING is a cold load
# of a 69 GB model (MEASURED at 198 s here, so the ceiling is deliberately generous for the
# same reason). DRAINING is llama-swap's 10 s graceful stop plus its escalation to a kill.
TTL = {
    Phase.PLANNED: timedelta(minutes=10),
    Phase.STARTING: timedelta(minutes=15),
    Phase.DRAINING: timedelta(minutes=5),
}


def is_abandoned(phase: Phase, sat_for: timedelta) -> bool:
    """Whether a reservation stuck in `phase` for `sat_for` should be swept.

    RESIDENT IS DELIBERATELY ABSENT FROM `TTL`, and this is the load-bearing half: a model can
    legitimately serve for weeks, so expiring a resident row would discharge memory the box is
    still holding — the ledger telling the exact lie it exists to prevent, and worse than
    having no ledger, because the number would be confidently wrong."""
    ttl = TTL.get(phase)
    return ttl is not None and sat_for > ttl


def reconcile_split(
    rows: Iterable[Reservation], resident: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Split a ledger against reality: (phantom instance ids, foreign served-model names).

    Two failures, and only one of them is the ledger's to fix.

    A PHANTOM is a row whose model is not running — a charge nobody will ever release. Left
    alone it shrinks the budget permanently and the box slowly refuses everything, so it is
    dropped.

    A FOREIGN model is running with no row: memory the ledger cannot see. It is NOT charged.
    Inventing a declaration for a process we did not admit would put a number we made up into
    the arithmetic that protects the box, and a made-up number that is too small is how a gate
    admits a load it should have refused. It is reported instead, so the caller can say so out
    loud — and the `min()` against the live measurement is what covers it meanwhile."""
    # `list(rows)`, because this reads it TWICE. Handed a generator, the second pass sees
    # nothing, `charged` comes back empty, and every resident model is reported as foreign —
    # a public function silently changing its answer with the caller's container type.
    rows = list(rows)
    resident = set(resident)
    phantoms = [r.instance_id for r in rows if r.served_model not in resident]
    charged = {r.served_model for r in rows}
    return phantoms, sorted(resident - charged)
