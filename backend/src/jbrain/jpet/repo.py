"""SQL JPet repository. Every query runs on an RLS-scoped session, so the domain
firewall (and the owner-only rule) is Postgres', not this module's — the same
pattern as lists/notes. W0 surface: ensure the single pet exists, read it, and
advance its drives on a tick."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.service import (
    Command,
    Drives,
    PetStateInfo,
    apply_command,
    decayed,
    mood_of,
)
from jbrain.models.jpet import PetMemory, PetState


def _info(row: PetState) -> PetStateInfo:
    return PetStateInfo(
        id=str(row.id),
        name=row.name,
        domain=row.domain_code,
        drives=Drives(food=row.food, energy=row.energy, fun=row.fun, love=row.love),
        mood=row.mood,
        emotion=row.emotion,
        speech=row.speech,
        asleep=row.asleep,
        pos_x=row.pos_x,
        pos_z=row.pos_z,
        target_x=row.target_x,
        target_z=row.target_z,
        facing=row.facing,
        action=row.action,
        last_tick_at=row.last_tick_at,
        updated_at=row.updated_at,
    )


class SqlJpetRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def get_pet(self, ctx: SessionContext, *, domain: str) -> PetStateInfo | None:
        """The pet in `domain`, or None when there is none / it's out of scope."""
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            return _info(row) if row is not None else None

    async def ensure_pet(self, ctx: SessionContext, *, name: str, domain: str) -> PetStateInfo:
        """Get-or-create the single pet for this owner in `domain`. Idempotent: the
        UNIQUE (principal_id, domain_code) constraint makes a lost create race re-read
        the winner. The insert runs in a SAVEPOINT so a conflict rolls back just that
        step and leaves the surrounding transaction usable for the re-read."""
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is not None:
                return _info(row)
            row = PetState(name=name, domain_code=domain, principal_id=_principal(ctx))
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                row = await self._load(session, domain)  # a concurrent tick won
                if row is None:  # pragma: no cover — a delete raced the insert
                    raise
            else:
                await session.refresh(row)
            return _info(row)

    async def tick(
        self, ctx: SessionContext, *, domain: str, now: datetime | None = None
    ) -> PetStateInfo | None:
        """Advance the pet's drives by the time elapsed since `last_tick_at`, recompute
        mood, and persist. Pure arithmetic — no LLM. None when there is no pet in scope.
        `now` is injectable for tests."""
        now = now or datetime.now(UTC)
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is None:
                return None
            dt = (now - row.last_tick_at).total_seconds()
            drives = decayed(
                Drives(food=row.food, energy=row.energy, fun=row.fun, love=row.love),
                dt,
                asleep=row.asleep,
            )
            mood = mood_of(drives, asleep=row.asleep)
            await session.execute(
                update(PetState)
                .where(PetState.id == row.id)
                .values(
                    food=drives.food,
                    energy=drives.energy,
                    fun=drives.fun,
                    love=drives.love,
                    mood=mood,
                    last_tick_at=now,
                    updated_at=func.now(),
                )
            )
            await session.refresh(row)
            return _info(row)

    async def apply_command(
        self, ctx: SessionContext, *, domain: str, command: Command
    ) -> PetStateInfo | None:
        """Fold a client command (feed/play/pet/poke/sleep/move) into the pet and
        persist. Adjusts drives/asleep/target/action/emotion and recomputes mood; does
        NOT touch `last_tick_at` (decay is the tick's job). None when no pet is in
        scope. The caller broadcasts the returned state to the surfaces."""
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is None:
                return None
            outcome = apply_command(
                drives=Drives(food=row.food, energy=row.energy, fun=row.fun, love=row.love),
                asleep=row.asleep,
                target_x=row.target_x,
                target_z=row.target_z,
                command=command,
            )
            await session.execute(
                update(PetState)
                .where(PetState.id == row.id)
                .values(
                    food=outcome.drives.food,
                    energy=outcome.drives.energy,
                    fun=outcome.drives.fun,
                    love=outcome.drives.love,
                    asleep=outcome.asleep,
                    emotion=outcome.emotion,
                    action=outcome.action,
                    target_x=outcome.target_x,
                    target_z=outcome.target_z,
                    mood=mood_of(outcome.drives, asleep=outcome.asleep),
                    updated_at=func.now(),
                )
            )
            await session.refresh(row)
            return _info(row)

    async def apply_reply(
        self, ctx: SessionContext, *, domain: str, speech: str, emotion: str, action: str
    ) -> PetStateInfo | None:
        """Persist a `pet.turn` reply — the current utterance + emotion/action — and
        recompute mood. None when no pet is in scope. The caller broadcasts."""
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is None:
                return None
            drives = Drives(food=row.food, energy=row.energy, fun=row.fun, love=row.love)
            await session.execute(
                update(PetState)
                .where(PetState.id == row.id)
                .values(
                    speech=speech,
                    emotion=emotion,
                    action=action,
                    asleep=False,
                    mood=mood_of(drives, asleep=False),
                    updated_at=func.now(),
                )
            )
            await session.refresh(row)
            return _info(row)

    async def record_memory(
        self, ctx: SessionContext, *, domain: str, kind: str, body: str
    ) -> None:
        """Append an episodic memory (a child's message, a care event). Best-effort —
        a memory write must never break the interaction it describes."""
        async with scoped_session(self._maker, ctx) as session:
            session.add(
                PetMemory(
                    domain_code=domain, principal_id=_principal(ctx), kind=kind, body=body[:400]
                )
            )

    async def recent_memories(
        self, ctx: SessionContext, *, domain: str, limit: int = 6
    ) -> list[str]:
        """The pet's most recent memories (newest first) for the `pet.turn` prompt."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(PetMemory.body)
                        .where(PetMemory.domain_code == domain)
                        .order_by(PetMemory.created_at.desc(), PetMemory.id.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    async def set_target(
        self, ctx: SessionContext, *, domain: str, x: float, z: float
    ) -> PetStateInfo | None:
        """Point the pet at a new floor target and set it walking — the autonomous
        wander step (docs/plans/JPET_PLAN.md W5). None when no pet is in scope."""
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is None:
                return None
            await session.execute(
                update(PetState)
                .where(PetState.id == row.id)
                .values(target_x=x, target_z=z, action="walk", updated_at=func.now())
            )
            await session.refresh(row)
            return _info(row)

    async def _load(self, session: AsyncSession, domain: str) -> PetState | None:
        return (
            await session.execute(select(PetState).where(PetState.domain_code == domain))
        ).scalar_one_or_none()


def _principal(ctx: SessionContext) -> uuid.UUID:
    """The owner principal id stamped on a new pet (RLS already proved owner)."""
    if ctx.principal_id is None:
        raise ValueError("a pet write needs an owner principal in context")
    return uuid.UUID(ctx.principal_id)
