"""The reservation ledger's storage: charge at intent, discharge at confirmed death.

`admission` holds the arithmetic and touches nothing; this holds the rows and the one
operation that has to be ATOMIC ACROSS PROCESSES — read the ledger, decide, and charge, with
no window in between for the api and the worker to both decide there is room.

That atomicity is the whole reason the ledger can be cheap. The cross-process box lock exists
today and is held across a model's entire load — 100-200 s on a request path, against a
15-connection pool with no `lock_timeout`. Here it is held for a SELECT and an INSERT:
milliseconds. The load itself then runs unlocked, protected by a row rather than by a lock.

Reservations are DELETED at discharge rather than marked dead. The absence of a row is what
"this instance is gone" means, and a tombstone phase would be one more state for a summation
to get wrong — which is the class of bug this whole design exists to remove."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import box_events
from jbrain.db.session import SessionContext, scoped_session
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import gpu_guard
from jbrain.llm.admission import (
    BOX_LOCK_KEY,
    Decision,
    Layer,
    Outcome,
    Phase,
    Pool,
    Reservation,
    admit,
    is_abandoned,
    reconcile_split,
)
from jbrain.models.telemetry import ModelReservation

log = structlog.get_logger()

# The box's own bookkeeping about its own hardware, written by both the api and the worker.
# Owner-kind because `app.is_owner()` keys on the KIND, not on a person — the same context
# `box_events` writes host telemetry under.
_CTX = SessionContext(principal_id="box", principal_kind="owner")

# How long a charge may wait for the box lock. Bounded because the same key is still held
# across a whole evict-and-load by `residency.ensure_room` (see `charge`), and an unbounded
# wait there would let fifteen queued charges consume the whole connection pool.
CHARGE_LOCK_TIMEOUT_MS = 15_000


@dataclass(frozen=True)
class Charge:
    """An admitted reservation: the decision, and the row it wrote."""

    decision: Decision
    instance_id: str | None  # None unless the decision admitted


def _phase(raw: str) -> Phase:
    """A stored phase, or RESIDENT for one this build does not know.

    The column is `text` so "a new phase must not need a migration to be observable" — but a
    strict `Phase(raw)` made an unknown value UNREADABLE instead of observable, and it is on the
    read path of every admission. A rolling deploy where the worker writes a phase the api has
    not learned yet would fail every model load in the api until both halves matched.

    RESIDENT is the safe landing: full charge, no TTL. An unknown phase is a reservation this
    build cannot reason about, and the only wrong answer for one of those is a smaller one."""
    try:
        return Phase(raw)
    except ValueError:
        log.warning("ledger.unknown_phase", phase=raw, treated_as=str(Phase.RESIDENT))
        return Phase.RESIDENT


def _to_reservation(row: ModelReservation) -> Reservation:
    return Reservation(
        instance_id=str(row.instance_id),
        served_model=row.served_model,
        phase=_phase(row.phase),
        host_gb=row.host_gb,
        device_gb=row.device_gb,
    )


class ReservationLedger:
    """The rows, and the atomic admit-and-charge.

    Constructed per process with that process's sessionmaker and its own name, so a phantom
    row can be traced to the half of the box that left it. Explicitly wired rather than an
    ambient module global: the residency wiring in this package has no defaults for exactly
    this reason — a half-wired gate that still compiles is what let an operator control be
    enforced on one coordinator and be invisible on another."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        source: str,
        device_probe: gpu_guard.GpuMemProbe | None = None,
        shadow: bool = True,
    ) -> None:
        self._maker = maker
        self._source = source
        self._device_probe = device_probe
        self._shadow = shadow
        # instance_id -> (served_model, host_gb, device_gb) for reservations this process
        # charged and has not released. Only so `advance` can re-charge at the ORIGINAL
        # declaration if something deletes the row underneath a live load; it is not a cache
        # and is never read for admission, which always reads the table.
        self._in_flight: dict[str, tuple[str, float, float]] = {}

    async def _pools(self) -> tuple[Pool, Pool]:
        """The two capacities, with what is currently measured of each.

        THE LEDGER IS THE READER. Putting `read_memory_gb` and `probe.sample()` here rather
        than at the call sites is the point of L3 arriving early: the box has had six
        independent memory budgets, each right about a different instant, and the way that ends
        is one place that reads and one place that decides. `test_memory_reader_inventory.py`
        is what keeps a seventh from appearing.

        A layer with nothing measured says None, which is NOT zero — zero is a reading, and it
        would refuse every load on a box whose probe merely went away. The ledger term still
        binds in that case."""
        mem = read_memory_gb()
        # None, NOT 0.0, when the read failed — the same rule `measured_free_gb` has always
        # had, and for a harder reason. A 0.0 capacity makes `usable` the negative reserve, so
        # `admit` calls every model INFEASIBLE ("never retry") and a transient unreadable
        # `/proc/meminfo` permanently fails every background job on the box, box-wide, with no
        # recovery when the file comes back.
        host = Pool(
            total_gb=mem[0] if mem else None,
            reserve_gb=gpu_guard.MIN_FREE_GTT_GB,
            measured_free_gb=(mem[0] - mem[1]) if mem else None,
        )
        sample = await self._device_probe.sample() if self._device_probe is not None else None
        device = Pool(
            total_gb=sample.gtt_total_gb if sample else (mem[0] if mem else None),
            reserve_gb=gpu_guard.MIN_FREE_GTT_GB,
            measured_free_gb=sample.gtt_free_gb if sample else None,
        )
        if host.measured_free_gb is None or device.measured_free_gb is None:
            # The measurement is the ONLY term that can see consumers the ledger did not
            # create, and this box has several — ComfyUI evicts LLMs and is never evicted by
            # them, whisper is a second llama-swap, Kokoro holds a model with no accounting in
            # `backend/src` at all. Falling back to the ledger term is the right call (refusing
            # every load because a probe went away would be worse), but it is a DEGRADED gate
            # and the reconciliation's "the min() covers foreign models meanwhile" stops being
            # true while it lasts. Never let that pass silently.
            log.warning(
                "ledger.pool_unmeasured",
                host_measured=host.measured_free_gb is not None,
                device_measured=device.measured_free_gb is not None,
            )
        return host, device

    async def charge(self, served_model: str, *, host_gb: float, device_gb: float) -> Charge:
        """Decide and, if admitted, insert — in ONE transaction holding the box lock.

        The lock is the BLOCKING form on purpose. Unlike a restore, an admission has nothing
        useful to do if it cannot get the lock: skipping would mean loading without deciding,
        which is the unguarded load this exists to prevent.

        WHAT IT WAITS BEHIND IS NOT THIS FUNCTION'S TO PROMISE, and an earlier version of this
        docstring promised it anyway — "it waits for a SELECT and an INSERT rather than for
        someone else's 200 s load". `residency.ensure_room`'s slow path still holds the SAME
        advisory key across a whole evict-and-load, which the plan's L1 item 5 leaves open by
        decision. So until that changes, a charge CAN queue behind a real load, and with a
        15-connection pool fifteen queued charges would be the api's whole capacity.

        Hence `lock_timeout`: the wait is bounded, and a timeout is reported rather than
        endured. In shadow it charges anyway and says so — an unmeasured box is not made safer
        by an api that stops answering. Once the ledger is authoritative a timeout must refuse,
        which is one more line in the same place, and by then item 5 should be closed.

        `host_gb`/`device_gb` are the caller's declaration, from `local_catalog.declared_gb`.
        They are written down verbatim and never recomputed for this instance's life."""
        host, device = await self._pools()
        instance_id = str(uuid.uuid4())
        async with scoped_session(self._maker, _CTX) as session:
            await session.execute(text(f"SET LOCAL lock_timeout = '{CHARGE_LOCK_TIMEOUT_MS}ms'"))
            try:
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": BOX_LOCK_KEY})
            except DBAPIError as exc:
                # The box lock is held by something long — see the docstring. Recorded loudly
                # because it means the ledger's decision was made against a census somebody
                # else may be changing, which is the one thing the lock was for.
                log.error("ledger.lock_timeout", model=served_model, error=str(exc))
                raise
            rows = [
                _to_reservation(r)
                for r in (await session.execute(select(ModelReservation))).scalars()
            ]
            request = Reservation(
                instance_id=instance_id,
                served_model=served_model,
                phase=Phase.PLANNED,
                host_gb=host_gb,
                device_gb=device_gb,
            )
            decision = admit(request, rows, host=host, device=device)
            if not decision.admitted:
                # SHADOW: the row is written anyway and the caller is told nothing, because a
                # ledger that has never charged a real load has no numbers to be judged on.
                # This repo has the precedent and the reason, in `_note_not_ready`'s own words:
                # "the fix removes the thing being measured, so shipping both together would
                # only ever report zero." So the lifecycle runs for real, the verdict is
                # recorded next to what the live gate actually did, and the box is observed
                # before it is governed. `shadow=False` is the whole of L2b.
                log.warning(
                    "ledger.shadow_would_refuse" if self._shadow else "ledger.refused",
                    model=served_model,
                    outcome=str(decision.outcome),
                    layer=str(decision.layer),
                    reason=decision.reason,
                )
                # ON THE SURFACE THE OWNER ACTUALLY READS, not only in a log line. They run
                # this box remotely with no terminal, so a verdict that only exists in the
                # container log is a verdict nobody can act on — and in shadow this verdict is
                # the whole evidence base for whether the ledger may start deciding.
                #
                # ABOVE the enforcing-mode return, not below it, which is where it used to sit:
                # the box narrated every refusal it did NOT act on and would have gone silent
                # on the ones it did, at exactly the moment a refusal starts costing a load.
                await box_events.record(
                    box_events.LEDGER_SHADOW_REFUSAL if self._shadow else box_events.LEDGER_REFUSAL,
                    served_model,
                    detail=decision.reason,
                    status="failed",
                )
                if not self._shadow:
                    return Charge(decision, None)
            session.add(
                ModelReservation(
                    instance_id=uuid.UUID(instance_id),
                    served_model=served_model,
                    phase=Phase.PLANNED.value,
                    host_gb=host_gb,
                    device_gb=device_gb,
                    source=self._source,
                )
            )
        self._in_flight[instance_id] = (served_model, host_gb, device_gb)
        log.info(
            "ledger.charged",
            model=served_model,
            instance=instance_id,
            host_gb=host_gb,
            device_gb=device_gb,
        )
        return Charge(decision, instance_id)

    async def advance(self, instance_id: str, phase: Phase) -> None:
        """Move one reservation to a later phase, restamping its TTL clock.

        The SIZES ARE NOT TOUCHED. A phase change is a statement about what the process is
        doing, never about what it costs — and in particular `DRAINING` keeps the full charge,
        because the kernel has freed nothing at the moment a stop is requested. Discharging on
        the shutdown INTENT is the one anti-pattern every prior-art scheduler names, and it is
        the one that OOMs a box.

        A MISSING ROW IS AN ERROR, NOT A NO-OP. This used to update zero rows in silence, and
        that silence is what would make a deleted reservation invisible: the load carries on,
        the model becomes resident, and the ledger charges nothing for it — a number that is
        confidently wrong, which is worse than no ledger at all. Advancing to RESIDENT is the
        moment the memory becomes permanent, so that case RE-CHARGES rather than only
        complaining: the box is holding it either way, and the ledger's job is to know."""
        async with scoped_session(self._maker, _CTX) as session:
            row = await session.get(ModelReservation, uuid.UUID(instance_id))
            if row is not None:
                row.phase = phase.value
                row.phase_at = datetime.now(UTC)
                return
            served, host_gb, device_gb = self._in_flight.get(instance_id, ("", 0.0, 0.0))
            log.error(
                "ledger.advance_lost_the_row",
                instance=instance_id,
                phase=str(phase),
                model=served,
                note="something deleted a live reservation — see reconcile() and sweep()",
            )
            if phase is Phase.RESIDENT and served:
                # Re-charge at the ORIGINAL declaration, not a fresh one: the arithmetic that
                # admitted this model is the arithmetic that has to protect it, and recomputing
                # would quietly change the promise mid-life.
                session.add(
                    ModelReservation(
                        instance_id=uuid.UUID(instance_id),
                        served_model=served,
                        phase=phase.value,
                        host_gb=host_gb,
                        device_gb=device_gb,
                        source=self._source,
                    )
                )
                log.warning("ledger.recharged_after_loss", instance=instance_id, model=served)

    async def discharge(self, instance_id: str | None) -> None:
        """Release a reservation. Call this ONLY on confirmed death — the process reaped, not
        merely asked to stop. llama-swap's `POST /api/models/unload/{model}` blocks until each
        targeted process has stopped, so a 200 from it is that confirmation.

        Tolerates None so the natural wiring — release whatever the charge returned, on every
        failure path — is not itself a crash on the path where the charge was refused."""
        if instance_id is None:
            return
        self._in_flight.pop(instance_id, None)
        async with scoped_session(self._maker, _CTX) as session:
            await session.execute(
                delete(ModelReservation).where(
                    ModelReservation.instance_id == uuid.UUID(instance_id)
                )
            )

    async def draining(self, served_model: str) -> list[str]:
        """Mark every reservation for this model as draining, KEEPING THE FULL CHARGE.

        By model name and not by instance, because that is what an unload actually does:
        `POST /api/models/unload/{model}` stops every process serving it. Returns the instance
        ids it moved, so a caller that then fails can tell what it was in the middle of."""
        async with scoped_session(self._maker, _CTX) as session:
            rows = list(
                (
                    await session.execute(
                        select(ModelReservation).where(
                            ModelReservation.served_model == served_model
                        )
                    )
                ).scalars()
            )
            for row in rows:
                row.phase = Phase.DRAINING.value
                row.phase_at = datetime.now(UTC)
            return [str(r.instance_id) for r in rows]

    async def discharge_model(self, served_model: str) -> int:
        """Release every reservation for this model. CONFIRMED DEATH ONLY.

        llama-swap's unload endpoint blocks until each targeted process has stopped
        (`internal/router/router.go`: "It blocks until each targeted process has stopped"), so a
        200 from it is that confirmation — the one place this codebase can honestly say the
        memory is back. A non-200 leaves the rows DRAINING, still charged, for the TTL sweep."""
        async with scoped_session(self._maker, _CTX) as session:
            rows = list(
                (
                    await session.execute(
                        select(ModelReservation).where(
                            ModelReservation.served_model == served_model
                        )
                    )
                ).scalars()
            )
            for row in rows:
                self._in_flight.pop(str(row.instance_id), None)
                await session.delete(row)
            return len(rows)

    async def live(self) -> list[Reservation]:
        """Every charged reservation. Used for narration and reconciliation; admission reads
        its own copy inside the charging transaction, because a read outside that lock is
        exactly the window the ledger exists to close."""
        async with scoped_session(self._maker, _CTX) as session:
            return [
                _to_reservation(r)
                for r in (await session.execute(select(ModelReservation))).scalars()
            ]

    async def sweep(self, *, now: datetime | None = None) -> list[str]:
        """Delete reservations that have sat too long in a transitional phase, and say which.

        A BACKSTOP for a process that died between charging and reaping, never the mechanism —
        every failure path rolls its own charge back. RESIDENT is deliberately absent from the
        TTL table: a model can legitimately serve for weeks, and sweeping one would discharge
        memory the box is still holding, which is the ledger telling the exact lie it exists to
        prevent. A row that fires here is a bug report, so it is logged at warning."""
        now = now or datetime.now(UTC)
        swept: list[str] = []
        async with scoped_session(self._maker, _CTX) as session:
            for row in (await session.execute(select(ModelReservation))).scalars():
                sat = row.phase_at
                if sat.tzinfo is None:
                    sat = sat.replace(tzinfo=UTC)
                if not is_abandoned(_phase(row.phase), now - sat):
                    continue
                log.warning(
                    "ledger.swept_abandoned",
                    model=row.served_model,
                    instance=str(row.instance_id),
                    phase=row.phase,
                    source=row.source,
                    sat_for_s=round((now - sat).total_seconds()),
                )
                swept.append(str(row.instance_id))
                await session.delete(row)
        return swept

    async def reconcile(self, resident: set[str]) -> tuple[list[str], list[str]]:
        """Startup: compare the ledger against what is actually running, and say what differs.

        Two failures, and only one of them is this ledger's to fix. A PHANTOM — a row whose
        model is not running — is a charge nobody will ever release, so it is deleted; left
        alone it shrinks the budget forever and the box slowly refuses everything. A FOREIGN
        model — running with no row — is memory the ledger cannot see, and it is NOT charged
        here: inventing a declaration for a process we did not admit would put a number we made
        up into the arithmetic that protects the box. It is reported so the caller can say so
        out loud, and the `min()` against the measurement is what covers it in the meantime.

        PHASE DECIDES WHAT MAY BE SWEPT, and getting this wrong is worse than not reconciling
        at all. LLAMA-SWAP OWNS THE MODEL PROCESSES: neither the api nor the worker restarting
        kills a single model, so "my process restarted, therefore my rows are dead" is false
        here, and so is any filter built on `source`. What IS conclusive is the phase:

        * RESIDENT or DRAINING and not running — it reached the box and is gone. A phantom.
        * PLANNED or STARTING and not running — that is what a load in progress LOOKS like. A
          cold load of a 69 GB model measured 198 s on this box, and for all of it the model is
          absent from `/running`. Sweeping those is the failure this guard exists for: the
          other process is mid-load, its charge is deleted, its `advance` to RESIDENT then
          matches no row, and the box ends up holding 68 GB the ledger has forgotten. The TTL
          sweep is what eventually collects one of these that really did die.

        Startup only, even so: mid-life, one bad `/running` reading — llama-swap reloading its
        config, a probe that timed out — would discharge a live model, and the ledger would
        then admit a second copy of it."""
        async with scoped_session(self._maker, _CTX) as session:
            by_id = {
                str(r.instance_id): r
                for r in (await session.execute(select(ModelReservation))).scalars()
            }
            everyone = [_to_reservation(r) for r in by_id.values()]
            # Settled rows only — see the docstring. FOREIGN is still computed against the
            # WHOLE ledger: a model another process is mid-loading is not unaccounted-for, and
            # reporting it as such would be this telling the operator the box is out of control
            # when it is not.
            settled = [r for r in everyone if r.phase in (Phase.RESIDENT, Phase.DRAINING)]
            phantoms, _ = reconcile_split(settled, resident)
            _, foreign = reconcile_split(everyone, resident)
            for instance_id in phantoms:
                row = by_id[instance_id]
                log.warning(
                    "ledger.phantom_reservation",
                    model=row.served_model,
                    instance=instance_id,
                    phase=row.phase,
                    source=row.source,
                )
                await session.delete(row)
        if foreign:
            log.warning("ledger.foreign_models", models=foreign)
        return phantoms, foreign


__all__ = ["Charge", "Decision", "Layer", "Outcome", "Phase", "Pool", "ReservationLedger"]
