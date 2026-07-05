"""The JPet ensure loop (docs/archive/JPET_V3_PLAN.md W1).

v3 moved the pet's continuous life onto the wall (a 60fps client sim). The server no
longer drives behaviour — this loop only makes sure the durable pet row EXISTS (so a
fresh box's wall has something to read) and keeps a liveness heartbeat. It runs in the
web process as a lightweight asyncio loop, deliberately NOT on the single-threaded job
queue, so the pet always takes second seat to real processing: it resolves the single
owner principal, ensures the pet, and touches `last_tick_at`. No LLM, no behaviour, no
decay — the wall owns all of that; the `pet.turn` talk brain runs on `/pet/command`.
"""

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.service import PetStateInfo

log = structlog.get_logger()

# A gentle cadence — the server only guarantees existence + a heartbeat now, so this can
# be slow and cheap. (The wall's own loop is what feels live.)
TICK_INTERVAL_SECONDS = 30.0

# The safe family domain the kids' pet lives in — never health/finance/location.
DEFAULT_PET_DOMAIN = "general"
DEFAULT_PET_NAME = "Blink"

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
    """Ensure the pet exists and refresh its heartbeat, publishing the state to
    `broadcaster`. Returns the state (used by tests; the loop ignores it), or None on a
    fresh box with no owner yet."""
    owner_pid = await _owner_principal_id(maker)
    if owner_pid is None:
        return None
    ctx = SessionContext(principal_id=str(owner_pid), principal_kind="owner")
    await repo.ensure_pet(ctx, name=name, domain=domain)
    state = await repo.tick(ctx, domain=domain)
    if state is not None and broadcaster is not None:
        broadcaster.publish(state)
    return state


async def run_jpet_loop(
    maker: async_sessionmaker[AsyncSession],
    repo: SqlJpetRepo,
    *,
    domain: str = DEFAULT_PET_DOMAIN,
    name: str = DEFAULT_PET_NAME,
    interval: float = TICK_INTERVAL_SECONDS,
    broadcaster: PetBroadcaster | None = None,
) -> None:
    """Drive `jpet_tick` forever on `interval`. A tick blip is logged and swallowed so a
    transient DB hiccup never kills the loop (mirrors the tasks loop's tolerance)."""
    while True:
        try:
            await jpet_tick(maker, repo, domain=domain, name=name, broadcaster=broadcaster)
        except Exception as exc:  # noqa: BLE001 — the tick must not kill the loop
            log.warning("jpet.tick_error", error=repr(exc))
        await asyncio.sleep(interval)
