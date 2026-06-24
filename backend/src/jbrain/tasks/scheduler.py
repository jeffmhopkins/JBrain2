"""The tasks scheduler tick + its background driver.

Tasks run in the web process (that's where the agent stack lives and where a
manual "Run now" already executes), so the tick is a lightweight asyncio loop
gated like the live-feed task — not the workflow worker. Each tick claims due
tasks (advancing their `next_run_at` under the lock so a re-entrant tick can't
double-fire), then runs each one via the `TaskRunner` outside the lock.

The tick resolves the single owner principal once to build the owner context the
runner needs (sessions/runs are owner-only). A fresh box with no owner is a no-op.
"""

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.tasks.repo import TaskRepo
from jbrain.tasks.runner import TaskRunner

log = structlog.get_logger()

# A minute cadence: task schedules are human-scale (a morning brief, a weekly
# review), so the tick need not be tighter than the smallest meaningful slot.
TICK_INTERVAL_SECONDS = 60.0

# A system owner context to read owner-only tables across all tasks. Owner identity
# (not a principal id) is what `is_owner()` checks; per-task runs use the task's own
# principal id (the runner creates the session under it).
_SYSTEM_OWNER = SessionContext(principal_kind="owner")


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    async with scoped_session(maker, _SYSTEM_OWNER) as session:
        sql = text("SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1")
        return (await session.execute(sql)).scalar()


async def tasks_tick(
    maker: async_sessionmaker[AsyncSession],
    repo: TaskRepo,
    runner: TaskRunner,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Claim and run every task due at `now`. Returns the ids of the runs started —
    used by tests; the loop ignores it. Never raises for a single task's failure (the
    runner records it as an error run)."""
    now = now or datetime.now(UTC)
    owner_pid = await _owner_principal_id(maker)
    if owner_pid is None:
        return []
    owner_ctx = SessionContext(principal_id=str(owner_pid), principal_kind="owner")
    due = await repo.claim_due(owner_ctx, now=now)
    started: list[str] = []
    for task in due:
        task_owner = SessionContext(principal_id=task.principal_id, principal_kind="owner")
        info = await runner.run(task_owner, task, trigger="schedule")
        await repo.mark_ran(task_owner, task.id, at=now)
        started.append(info.id)
    return started


async def run_tasks_loop(
    maker: async_sessionmaker[AsyncSession],
    repo: TaskRepo,
    runner: TaskRunner,
    *,
    interval: float = TICK_INTERVAL_SECONDS,
) -> None:
    """Drive `tasks_tick` forever on `interval`. A tick blip is logged and swallowed
    so a transient DB/LLM hiccup never kills the loop (mirrors the worker's tolerance)."""
    while True:
        try:
            await tasks_tick(maker, repo, runner)
        except Exception as exc:  # noqa: BLE001 — the tick must not kill the loop
            log.warning("tasks.tick_error", error=repr(exc))
        await asyncio.sleep(interval)
