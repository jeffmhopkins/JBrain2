"""The JPet drives tick + its background driver (docs/plans/JPET_PLAN.md §4–§5).

The tick runs in the web process as a lightweight asyncio loop — deliberately NOT
on the single-threaded job queue — so the pet always takes second seat to real
processing: it is a few multiplies and one UPDATE, never enqueued. It resolves the
single owner principal once (a fresh box with no owner is a no-op), ensures the pet
exists, and advances it (v2: positive meters + a capped ambient play beat). No LLM
here — the tick is pure arithmetic; the `pet.turn` talk brain runs on `/pet/command`.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.service import LOCATIONS, OBJECTS, PetStateInfo, Step, clean_script

log = structlog.get_logger()

# A brisker cadence than the tasks tick: the pet's position/mood should feel live, not
# human-scale. Still trivially cheap.
TICK_INTERVAL_SECONDS = 30.0

# The safe family domain the kids' pet lives in — never health/finance/location.
DEFAULT_PET_DOMAIN = "general"
DEFAULT_PET_NAME = "Blink"

# Chance per tick that an awake, un-busy pet does a little ambient thing on its own — so
# it feels alive on the Wall with no interaction. Capped autonomy (Neko/Sims-style): it
# skips its turn if a child just gave a command, so a kid always wins.
WANDER_CHANCE = 0.5
# Ambient behaviour is suppressed for this long after a command, so idle life never
# interrupts a script a child just started.
COMMAND_QUIET_SECONDS = 12.0


def _ambient_script(objects: dict[str, tuple[float, float]]) -> list[Step]:
    """A gentle, no-reward idle behaviour: mosey to a random room object or named spot
    and do a small expressive beat. `secrets` (Math.random-free) — the values are
    cosmetic, not security-sensitive. Cleaned so it stays bounded + terminating."""
    spots = [o for o in OBJECTS if o in objects and o != "light_switch"] + list(LOCATIONS)
    where = spots[secrets.randbelow(len(spots))]
    beat = ("wiggle", "spin", "nod", "look_at")[secrets.randbelow(4)]
    go = (
        {"action": "go_to", "target": where}
        if where in objects
        else {"action": "go_to", "destination": where}
    )
    raw = [go, {"action": beat, "duration_ms": 900}, {"action": "idle"}]
    return clean_script(raw, objects=objects)


# A system owner context to resolve the owner principal (owner identity is what
# is_owner() checks); the pet row itself is stamped with that principal id.
_SYSTEM_OWNER = SessionContext(principal_kind="owner")


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    async with scoped_session(maker, _SYSTEM_OWNER) as session:
        sql = text("SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1")
        return (await session.execute(sql)).scalar()


async def jpet_tick(
    maker: async_sessionmaker[AsyncSession],
    repo: SqlJpetRepo,
    *,
    domain: str = DEFAULT_PET_DOMAIN,
    name: str = DEFAULT_PET_NAME,
    broadcaster: PetBroadcaster | None = None,
) -> PetStateInfo | None:
    """Ensure the pet exists and advance it once (positive meters, maybe an ambient beat),
    publishing the new state to `broadcaster` so the surfaces see it live. Returns the new
    state (used by tests; the loop ignores it), or None on a fresh box with no owner yet."""
    owner_pid = await _owner_principal_id(maker)
    if owner_pid is None:
        return None
    ctx = SessionContext(principal_id=str(owner_pid), principal_kind="owner")
    await repo.ensure_pet(ctx, name=name, domain=domain)
    state = await repo.tick(ctx, domain=domain)
    # Ambient life: an awake, un-busy pet occasionally moseys somewhere on its own. Capped
    # so it never overrides a command a child just gave (COMMAND_QUIET_SECONDS).
    if state is not None and not state.asleep and _may_wander(state):
        ambient = await repo.run_script(
            ctx, domain=domain, script=_ambient_script(state.objects), reward=False
        )
        if ambient is not None:
            state = ambient
    if state is not None and broadcaster is not None:
        broadcaster.publish(state)
    return state


def _may_wander(state: PetStateInfo) -> bool:
    """Roll the ambient-behaviour dice, but only when a script isn't still fresh — a
    just-issued script (its `script_started_at`) is left to play out undisturbed for a few
    seconds, so idle life never cuts off something a child just started."""
    started = state.script_started_at
    if started is not None and datetime.now(UTC) - started < timedelta(
        seconds=COMMAND_QUIET_SECONDS
    ):
        return False
    return secrets.randbelow(1000) < WANDER_CHANCE * 1000


async def run_jpet_loop(
    maker: async_sessionmaker[AsyncSession],
    repo: SqlJpetRepo,
    *,
    domain: str = DEFAULT_PET_DOMAIN,
    name: str = DEFAULT_PET_NAME,
    interval: float = TICK_INTERVAL_SECONDS,
    broadcaster: PetBroadcaster | None = None,
) -> None:
    """Drive `jpet_tick` forever on `interval`. A tick blip is logged and swallowed so
    a transient DB hiccup never kills the loop (mirrors the tasks loop's tolerance)."""
    while True:
        try:
            await jpet_tick(maker, repo, domain=domain, name=name, broadcaster=broadcaster)
        except Exception as exc:  # noqa: BLE001 — the tick must not kill the loop
            log.warning("jpet.tick_error", error=repr(exc))
        await asyncio.sleep(interval)
