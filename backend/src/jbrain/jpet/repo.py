"""SQL JPet repository. Every query runs on an RLS-scoped session, so the domain
firewall (and the owner-only rule) is Postgres', not this module's — the same pattern
as lists/notes. v2 (docs/proposed/JPET_V2_PLAN.md) persists the pet's action *script*
and the room objects it targets/carries; the pet's play stays pure arithmetic, no LLM
in the tick (still second seat)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.service import (
    OBJECT_HOMES,
    Drives,
    PetStateInfo,
    Step,
    apply_play_reward,
    decayed,
    mood_of,
    settle_script,
)
from jbrain.models.jpet import PetMemory, PetState


def _objects_from_row(row: PetState) -> dict[str, tuple[float, float]]:
    """Row jsonb `{kind: [x, z]}` → the typed dict the service math uses. Falls back to
    the object homes for a pre-v2 row whose objects were never seeded."""
    raw = row.objects or {}
    out: dict[str, tuple[float, float]] = {}
    for kind, xz in raw.items():
        if isinstance(xz, (list, tuple)) and len(xz) == 2:
            out[str(kind)] = (float(xz[0]), float(xz[1]))
    return out or {k: (v[0], v[1]) for k, v in OBJECT_HOMES.items()}


def _objects_to_json(objects: dict[str, tuple[float, float]]) -> dict[str, list[float]]:
    return {k: [round(v[0], 4), round(v[1], 4)] for k, v in objects.items()}


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
        script=list(row.script or []),
        script_started_at=row.script_started_at,
        carrying=row.carrying,
        lights_on=row.lights_on,
        objects=_objects_from_row(row),
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
        """Get-or-create the single pet for this owner in `domain`. Idempotent via the
        UNIQUE (principal_id, domain_code) constraint. Seeds the room objects on create
        (and backfills them for a pre-v2 row whose objects are still empty)."""
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is not None:
                if not row.objects:  # pre-v2 row — seed the room so targeting works
                    await session.execute(
                        update(PetState)
                        .where(PetState.id == row.id)
                        .values(objects=_objects_to_json(dict(OBJECT_HOMES)))
                    )
                    await session.refresh(row)
                return _info(row)
            row = PetState(
                name=name,
                domain_code=domain,
                principal_id=_principal(ctx),
                objects=_objects_to_json(dict(OBJECT_HOMES)),
            )
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
        """Advance the pet once and persist. v2: the happy meters do NOT decay (only a
        napping pet recovers energy); mood is always positive. Pure arithmetic — no LLM.
        None when there is no pet in scope. `now` is injectable for tests."""
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

    async def run_script(
        self,
        ctx: SessionContext,
        *,
        domain: str,
        script: list[Step],
        speech: str | None = None,
        emotion: str | None = None,
        reward: bool = True,
        now: datetime | None = None,
    ) -> PetStateInfo | None:
        """Settle a (already-cleaned) action script into the pet + room and persist it: the
        pet's resting pose, what it now carries, the room objects' new positions, the light
        state, and the script itself + its start time (the wall replays the motion from
        there). Optionally sets the utterance/emotion (the `say` path) and applies the
        one-directional play reward. Does NOT touch `last_tick_at`. None when no pet in
        scope; the caller broadcasts."""
        now = now or datetime.now(UTC)
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is None:
                return None
            objects = _objects_from_row(row)
            settled = settle_script(
                pos_x=row.pos_x,
                pos_z=row.pos_z,
                facing=row.facing,
                asleep=row.asleep,
                carrying=row.carrying,
                lights_on=row.lights_on,
                objects=objects,
                script=script,
            )
            drives = Drives(food=row.food, energy=row.energy, fun=row.fun, love=row.love)
            if reward:
                drives = apply_play_reward(drives)
            values: dict[str, Any] = {
                "script": [s.as_dict() for s in script],
                "script_started_at": now,
                "action": settled.action,
                "pos_x": settled.pos_x,
                "pos_z": settled.pos_z,
                "target_x": settled.pos_x,
                "target_z": settled.pos_z,
                "facing": settled.facing,
                "asleep": settled.asleep,
                "carrying": settled.carrying,
                "lights_on": settled.lights_on,
                "objects": _objects_to_json(settled.objects),
                "food": drives.food,
                "energy": drives.energy,
                "fun": drives.fun,
                "love": drives.love,
                "mood": mood_of(drives, asleep=settled.asleep),
                "updated_at": func.now(),
            }
            if speech is not None:
                values["speech"] = speech
            if emotion is not None:
                values["emotion"] = emotion
            await session.execute(update(PetState).where(PetState.id == row.id).values(**values))
            await session.refresh(row)
            return _info(row)

    async def move_to(
        self, ctx: SessionContext, *, domain: str, x: float, z: float, now: datetime | None = None
    ) -> PetStateInfo | None:
        """Send the pet to a raw floor point (the parent room-map affordance) — a plain
        walk, no script. Clears any script so the wall falls back to its target-walk, and
        carries a held object along. None when no pet in scope; the caller broadcasts."""
        now = now or datetime.now(UTC)
        x, z = max(-1.0, min(1.0, x)), max(-1.0, min(1.0, z))
        async with scoped_session(self._maker, ctx) as session:
            row = await self._load(session, domain)
            if row is None:
                return None
            values: dict[str, Any] = {
                "script": [],
                "script_started_at": now,
                "action": "walk",
                "asleep": False,
                "target_x": x,
                "target_z": z,
                "updated_at": func.now(),
            }
            if row.carrying:  # a carried object rides to the destination
                objects = _objects_from_row(row)
                objects[row.carrying] = (x, z)
                values["objects"] = _objects_to_json(objects)
            await session.execute(update(PetState).where(PetState.id == row.id).values(**values))
            await session.refresh(row)
            return _info(row)

    async def record_memory(
        self, ctx: SessionContext, *, domain: str, kind: str, body: str
    ) -> None:
        """Append an episodic memory (a child's message, a play event). Best-effort — a
        memory write must never break the interaction it describes."""
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

    async def _load(self, session: AsyncSession, domain: str) -> PetState | None:
        return (
            await session.execute(select(PetState).where(PetState.domain_code == domain))
        ).scalar_one_or_none()


def _principal(ctx: SessionContext) -> uuid.UUID:
    """The owner principal id stamped on a new pet (RLS already proved owner)."""
    if ctx.principal_id is None:
        raise ValueError("a pet write needs an owner principal in context")
    return uuid.UUID(ctx.principal_id)
