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
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
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


@dataclass(frozen=True)
class Charge:
    """An admitted reservation: the decision, and the row it wrote."""

    decision: Decision
    instance_id: str | None  # None unless the decision admitted


def _to_reservation(row: ModelReservation) -> Reservation:
    return Reservation(
        instance_id=str(row.instance_id),
        served_model=row.served_model,
        phase=Phase(row.phase),
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

    def __init__(self, maker: async_sessionmaker[AsyncSession], *, source: str) -> None:
        self._maker = maker
        self._source = source

    async def charge(
        self,
        served_model: str,
        *,
        host_gb: float,
        device_gb: float,
        host: Pool,
        device: Pool,
    ) -> Charge:
        """Decide and, if admitted, insert — in ONE transaction holding the box lock.

        The lock is the BLOCKING form on purpose. Unlike a restore, an admission has nothing
        useful to do if it cannot get the lock: skipping would mean loading without deciding,
        which is the unguarded load this exists to prevent. It waits, and it waits for a SELECT
        and an INSERT rather than for someone else's 200 s load.

        `host_gb`/`device_gb` are the caller's declaration, from `local_catalog.declared_gb`.
        They are written down verbatim and never recomputed for this instance's life."""
        instance_id = str(uuid.uuid4())
        async with scoped_session(self._maker, _CTX) as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": BOX_LOCK_KEY})
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
                log.info(
                    "ledger.refused",
                    model=served_model,
                    outcome=str(decision.outcome),
                    layer=str(decision.layer),
                    reason=decision.reason,
                )
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
        the one that OOMs a box."""
        async with scoped_session(self._maker, _CTX) as session:
            await session.execute(
                update(ModelReservation)
                .where(ModelReservation.instance_id == uuid.UUID(instance_id))
                .values(phase=phase.value, phase_at=datetime.now(UTC))
            )

    async def discharge(self, instance_id: str) -> None:
        """Release a reservation. Call this ONLY on confirmed death — the process reaped, not
        merely asked to stop. llama-swap's `POST /api/models/unload/{model}` blocks until each
        targeted process has stopped, so a 200 from it is that confirmation."""
        async with scoped_session(self._maker, _CTX) as session:
            await session.execute(
                delete(ModelReservation).where(
                    ModelReservation.instance_id == uuid.UUID(instance_id)
                )
            )

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
                if not is_abandoned(Phase(row.phase), now - sat):
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

        STARTUP ONLY, and the distinction matters: a RESIDENT row is swept here because the
        process it described died with the last run of this process, which is the case
        reconciliation exists for. Running this mid-life would discharge a live model on one
        bad `/running` reading — llama-swap reloading its config, a probe that timed out — and
        the ledger would then admit a second copy of it."""
        async with scoped_session(self._maker, _CTX) as session:
            by_id = {
                str(r.instance_id): r
                for r in (await session.execute(select(ModelReservation))).scalars()
            }
            phantoms, foreign = reconcile_split(
                [_to_reservation(r) for r in by_id.values()], resident
            )
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
