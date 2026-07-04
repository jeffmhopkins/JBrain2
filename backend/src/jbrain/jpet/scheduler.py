"""The JPet drives tick + its background driver (docs/plans/JPET_PLAN.md §4–§5).

The tick runs in the web process as a lightweight asyncio loop — deliberately NOT
on the single-threaded job queue — so the pet always takes second seat to real
processing: it is a few multiplies and one UPDATE, never enqueued. It resolves the
single owner principal once (a fresh box with no owner is a no-op), ensures the pet
exists, and advances its drives. No LLM here — the tick is pure arithmetic; the
`pet.turn`/`pet.thought` calls land in later waves.
"""

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.service import PetStateInfo

log = structlog.get_logger()

# A brisker cadence than the tasks tick: the pet's drives (and, in later waves, its
# position) should feel live, not human-scale. Still trivially cheap.
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
) -> PetStateInfo | None:
    """Ensure the pet exists and advance its drives once. Returns the new state (used
    by tests; the loop ignores it), or None on a fresh box with no owner yet."""
    owner_pid = await _owner_principal_id(maker)
    if owner_pid is None:
        return None
    ctx = SessionContext(principal_id=str(owner_pid), principal_kind="owner")
    await repo.ensure_pet(ctx, name=name, domain=domain)
    return await repo.tick(ctx, domain=domain)


async def run_jpet_loop(
    maker: async_sessionmaker[AsyncSession],
    repo: SqlJpetRepo,
    *,
    domain: str = DEFAULT_PET_DOMAIN,
    name: str = DEFAULT_PET_NAME,
    interval: float = TICK_INTERVAL_SECONDS,
) -> None:
    """Drive `jpet_tick` forever on `interval`. A tick blip is logged and swallowed so
    a transient DB hiccup never kills the loop (mirrors the tasks loop's tolerance)."""
    while True:
        try:
            await jpet_tick(maker, repo, domain=domain, name=name)
        except Exception as exc:  # noqa: BLE001 — the tick must not kill the loop
            log.warning("jpet.tick_error", error=repr(exc))
        await asyncio.sleep(interval)
