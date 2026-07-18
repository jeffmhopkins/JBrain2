"""Owner-scoped CRUD for tasks + task runs, on RLS-scoped sessions.

Tasks are owner-only metadata (RLS `is_owner()`), so every query flows through
`scoped_session` under an owner context. `next_run_at` is recomputed on create /
update / claim from the schedule spec via `tasks.schedule.next_run_after`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.tasks import Task, TaskGroup, TaskRun
from jbrain.tasks.schedule import next_run_after, spec_from

_SUMMARY_LEN = 240


@dataclass(frozen=True)
class TaskGroupInfo:
    id: str
    name: str
    position: int


@dataclass(frozen=True)
class TaskInfo:
    id: str
    principal_id: str
    group_id: str | None
    position: int
    name: str
    prompt: str
    agent: str
    domain_scopes: tuple[str, ...]
    schedule_kind: str
    schedule_freq: str | None
    schedule_days: tuple[int, ...]
    schedule_time: str | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    notify_push: bool
    home_card: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TaskRunInfo:
    id: str
    task_id: str
    session_id: str | None
    run_id: str | None
    status: str
    trigger: str
    summary: str
    error: str | None
    step_count: int
    cost_tokens: int
    started_at: datetime
    ended_at: datetime | None


def _info(row: Task) -> TaskInfo:
    return TaskInfo(
        id=str(row.id),
        principal_id=str(row.principal_id),
        group_id=str(row.group_id) if row.group_id is not None else None,
        position=row.position,
        name=row.name,
        prompt=row.prompt,
        agent=row.agent,
        domain_scopes=tuple(row.domain_scopes),
        schedule_kind=row.schedule_kind,
        schedule_freq=row.schedule_freq,
        schedule_days=tuple(row.schedule_days),
        schedule_time=row.schedule_time,
        run_at=row.run_at,
        timezone=row.timezone,
        enabled=row.enabled,
        notify_push=row.notify_push,
        home_card=row.home_card,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _run_info(row: TaskRun) -> TaskRunInfo:
    return TaskRunInfo(
        id=str(row.id),
        task_id=str(row.task_id),
        session_id=str(row.session_id) if row.session_id is not None else None,
        run_id=str(row.run_id) if row.run_id is not None else None,
        status=row.status,
        trigger=row.trigger,
        summary=row.summary,
        error=row.error,
        step_count=row.step_count,
        cost_tokens=row.cost_tokens,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _compute_next(row_or_fields: TaskInfo | Task, *, now: datetime) -> datetime | None:
    """The next fire instant for a task from its (already-validated) schedule fields.
    Disabled tasks never fire; the schedule module decides the rest."""
    enabled = row_or_fields.enabled
    if not enabled:
        return None
    spec = spec_from(
        kind=row_or_fields.schedule_kind,
        freq=row_or_fields.schedule_freq,
        days=row_or_fields.schedule_days,
        time=row_or_fields.schedule_time,
        run_at=row_or_fields.run_at,
        tz=row_or_fields.timezone,
    )
    return next_run_after(spec, now)


async def _next_position(session: AsyncSession, principal_id: str, group_id: str | None) -> int:
    """The append index for a new task in `group_id` (NULL = Ungrouped): one past the
    current max within that bucket, or 0 when the bucket is empty."""
    gid = uuid.UUID(group_id) if group_id else None
    cond = Task.group_id.is_(None) if gid is None else (Task.group_id == gid)
    top = (
        await session.execute(select(func.max(Task.position)).where(cond))
    ).scalar()
    return 0 if top is None else int(top) + 1


class TaskGroupRepo:
    """Owner-scoped CRUD for the task buckets (RLS `is_owner()`). Groups carry only a
    name + display `position`; task membership lives on `Task.group_id`."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    def _group_info(self, row: TaskGroup) -> TaskGroupInfo:
        return TaskGroupInfo(id=str(row.id), name=row.name, position=row.position)

    async def list(self, ctx: SessionContext) -> list[TaskGroupInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(TaskGroup).order_by(TaskGroup.position, TaskGroup.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [self._group_info(r) for r in rows]

    async def create(self, ctx: SessionContext, *, name: str) -> TaskGroupInfo:
        async with scoped_session(self._maker, ctx) as session:
            top = (
                await session.execute(select(func.max(TaskGroup.position)))
            ).scalar()
            row = TaskGroup(
                principal_id=uuid.UUID(ctx.principal_id),
                name=name,
                position=0 if top is None else int(top) + 1,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return self._group_info(row)

    async def rename(
        self, ctx: SessionContext, group_id: str, *, name: str
    ) -> TaskGroupInfo | None:
        now = datetime.now(UTC)
        async with scoped_session(self._maker, ctx) as session:
            row = await session.get(TaskGroup, uuid.UUID(group_id))
            if row is None:
                return None
            row.name = name
            row.updated_at = now
            await session.flush()
            await session.refresh(row)
            return self._group_info(row)

    async def delete(self, ctx: SessionContext, group_id: str) -> None:
        # Tasks in the group are SET NULL by the FK (they fall to Ungrouped), never
        # deleted — deleting a bucket must not lose the owner's tasks.
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(delete(TaskGroup).where(TaskGroup.id == uuid.UUID(group_id)))


class TaskRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def create(
        self,
        ctx: SessionContext,
        *,
        name: str,
        prompt: str,
        agent: str,
        domain_scopes: Sequence[str],
        schedule_kind: str,
        schedule_freq: str | None,
        schedule_days: Sequence[int],
        schedule_time: str | None,
        run_at: datetime | None,
        timezone: str,
        enabled: bool = True,
        notify_push: bool = True,
        home_card: bool = True,
        now: datetime | None = None,
    ) -> TaskInfo:
        now = now or datetime.now(UTC)
        row = Task(
            principal_id=uuid.UUID(ctx.principal_id),
            name=name,
            prompt=prompt,
            agent=agent,
            domain_scopes=list(domain_scopes),
            schedule_kind=schedule_kind,
            schedule_freq=schedule_freq,
            schedule_days=list(schedule_days),
            schedule_time=schedule_time,
            run_at=run_at,
            timezone=timezone,
            enabled=enabled,
            notify_push=notify_push,
            home_card=home_card,
        )
        row.next_run_at = _compute_next(_info(row), now=now) if enabled else None
        async with scoped_session(self._maker, ctx) as session:
            # A new task appends to the end of the Ungrouped bucket (group_id NULL).
            row.position = await _next_position(session, ctx.principal_id, None)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def list(self, ctx: SessionContext) -> list[TaskInfo]:
        # Ordered by intra-group position first (the persisted reorder), then newest
        # for the tiebreak; the client buckets by group, so cross-group interleaving
        # of equal positions is immaterial.
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(Task).order_by(Task.position, Task.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_info(r) for r in rows]

    async def reorder(
        self, ctx: SessionContext, *, group_id: str | None, task_ids: Sequence[str]
    ) -> list[TaskInfo]:
        """Set the authoritative membership + order for one group's list: each id in
        `task_ids` is moved into `group_id` (NULL = Ungrouped) at its list index. This
        one call serves both a within-group reorder and a "Move to…" (the client sends
        the destination group's full ordered id list with the moved task appended).
        Ids not owned by the caller are silently skipped (RLS hides them)."""
        gid = uuid.UUID(group_id) if group_id else None
        moved: list[TaskInfo] = []
        async with scoped_session(self._maker, ctx) as session:
            if gid is not None:  # a real group must exist and belong to the owner
                grp = await session.get(TaskGroup, gid)
                if grp is None:
                    return []
            for i, tid in enumerate(task_ids):
                row = await session.get(Task, uuid.UUID(tid))
                if row is None:
                    continue
                row.group_id = gid
                row.position = i
                moved.append(_info(row))
            await session.flush()
        return moved

    async def get(self, ctx: SessionContext, task_id: str) -> TaskInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = await session.get(Task, uuid.UUID(task_id))
            return _info(row) if row is not None else None

    async def update(
        self, ctx: SessionContext, task_id: str, *, now: datetime | None = None, **fields: object
    ) -> TaskInfo | None:
        """Patch the given columns, then recompute `next_run_at` from the resulting
        schedule. Unknown keys are ignored; only present keys are written."""
        now = now or datetime.now(UTC)
        cols = {
            k: fields[k]
            for k in (
                "name",
                "prompt",
                "agent",
                "domain_scopes",
                "schedule_kind",
                "schedule_freq",
                "schedule_days",
                "schedule_time",
                "run_at",
                "timezone",
                "enabled",
                "notify_push",
                "home_card",
            )
            if k in fields
        }
        async with scoped_session(self._maker, ctx) as session:
            row = await session.get(Task, uuid.UUID(task_id))
            if row is None:
                return None
            for k, v in cols.items():
                setattr(row, k, list(v) if isinstance(v, (list, tuple)) else v)
            row.updated_at = now
            row.next_run_at = _compute_next(_info(row), now=now)
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def delete(self, ctx: SessionContext, task_id: str) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(delete(Task).where(Task.id == uuid.UUID(task_id)))

    async def claim_due(self, ctx: SessionContext, *, now: datetime) -> list[TaskInfo]:
        """Atomically claim every enabled task due at `now` and advance each one's
        `next_run_at` before releasing the lock, so a concurrent ticker can't double-
        fire it. The heavy agent run happens *after* this returns, outside the lock.
        A one-off advances to NULL (it has no next)."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.enabled.is_(True), Task.next_run_at <= now)
                        .order_by(Task.next_run_at)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            claimed = [_info(r) for r in rows]
            for r in rows:
                r.next_run_at = _compute_next(_info(r), now=now)
            return claimed

    async def mark_ran(self, ctx: SessionContext, task_id: str, *, at: datetime) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(Task).where(Task.id == uuid.UUID(task_id)).values(last_run_at=at)
            )


class TaskRunRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def start(
        self,
        ctx: SessionContext,
        *,
        task_id: str,
        principal_id: str,
        session_id: str | None,
        run_id: str | None,
        trigger: str,
    ) -> str:
        async with scoped_session(self._maker, ctx) as session:
            row = TaskRun(
                task_id=uuid.UUID(task_id),
                principal_id=uuid.UUID(principal_id),
                session_id=uuid.UUID(session_id) if session_id else None,
                run_id=uuid.UUID(run_id) if run_id else None,
                status="running",
                trigger=trigger,
            )
            session.add(row)
            await session.flush()
            return str(row.id)

    async def finish(
        self,
        ctx: SessionContext,
        run_id: str,
        *,
        status: str,
        summary: str = "",
        error: str | None = None,
        step_count: int = 0,
        cost_tokens: int = 0,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(TaskRun)
                .where(TaskRun.id == uuid.UUID(run_id))
                .values(
                    status=status,
                    summary=summary[:_SUMMARY_LEN],
                    error=error,
                    step_count=step_count,
                    cost_tokens=cost_tokens,
                    ended_at=datetime.now(UTC),
                )
            )

    async def latest_per_task(
        self, ctx: SessionContext, task_ids: Sequence[str]
    ) -> dict[str, TaskRunInfo]:
        """The most recent FINISHED run for each of `task_ids` (by `started_at`),
        keyed by task id. Tasks that have never run — or whose only run is still in
        flight — are absent. Backs the Tasks card's always-visible "latest result"
        band, so the newest session is one tap away without expanding the card. An
        in-flight run (NULL `ended_at`) is excluded, so the band keeps showing the
        last completed result until the new run's turn finishes (not on start)."""
        if not task_ids:
            return {}
        ids = [uuid.UUID(t) for t in task_ids]
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(TaskRun)
                        .where(TaskRun.task_id.in_(ids), TaskRun.ended_at.isnot(None))
                        .order_by(TaskRun.task_id, TaskRun.started_at.desc())
                        .distinct(TaskRun.task_id)
                    )
                )
                .scalars()
                .all()
            )
            return {str(r.task_id): _run_info(r) for r in rows}

    async def list_for_task(
        self, ctx: SessionContext, task_id: str, *, limit: int = 20
    ) -> list[TaskRunInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(TaskRun)
                        .where(TaskRun.task_id == uuid.UUID(task_id))
                        .order_by(TaskRun.started_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_run_info(r) for r in rows]
