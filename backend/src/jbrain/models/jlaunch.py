"""jlaunch run-index ORM + repo (migration 0152).

`jlaunch_runs` is the owner-only durable mirror of the job launcher (mirrors
`jcode_sessions`): one row per launched computation, holding the state/phase the launcher
lists plus the finished-run snapshot the public results page reads (headline block, machine
specs) and the artifact pointer (content-addressed sha + size) the download streams. The
job's real state — the live PTY and the checkout — lives in the jlaunch control server;
this table is the durable metadata the PWA (and a shared results page) read. NOT the owner's
knowledge base; no domain, no notes/entities.

`JlaunchRunRepo` takes the caller's already-RLS-scoped `AsyncSession` directly, so the
owner-only firewall is Postgres', not these methods'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Text, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class JlaunchRun(Base):
    """One launched computation's durable metadata + finished-run snapshot."""

    __tablename__ = "jlaunch_runs"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    spec_name: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    repo: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(Text, default="running")
    phase: Mapped[str] = mapped_column(Text, default="")
    verify_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Snapshots read by the public results page — the headline lines the run printed and the
    # machine specs it ran on. JSONB so the shape can evolve without a migration.
    headline: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    machine: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    phase_times: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    # The collected artifact: its filename, content-addressed sha (into the blob store), and
    # size. Set at mint time when the api streams the tarball out of the control server.
    artifact_name: Mapped[str] = mapped_column(Text, default="")
    artifact_sha256: Mapped[str] = mapped_column(Text, default="")
    artifact_bytes: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


@dataclass(frozen=True)
class JlaunchRunRow:
    """A plain view of a run row, safe to return from a route."""

    id: str
    spec_name: str
    title: str
    repo: str
    state: str
    phase: str
    verify_ok: bool | None
    headline: list[Any]
    machine: dict[str, Any]
    phase_times: list[Any]
    error: str
    artifact_name: str
    artifact_sha256: str
    artifact_bytes: int | None
    created_at: str
    last_active_at: str

    @classmethod
    def of(cls, row: JlaunchRun) -> JlaunchRunRow:
        return cls(
            id=row.id,
            spec_name=row.spec_name,
            title=row.title,
            repo=row.repo,
            state=row.state,
            phase=row.phase,
            verify_ok=row.verify_ok,
            headline=list(row.headline or []),
            machine=dict(row.machine or {}),
            phase_times=list(row.phase_times or []),
            error=row.error,
            artifact_name=row.artifact_name,
            artifact_sha256=row.artifact_sha256,
            artifact_bytes=row.artifact_bytes,
            created_at=row.created_at.isoformat(),
            last_active_at=row.last_active_at.isoformat(),
        )


# The control-job fields mirrored onto a run row (everything but the artifact pointer,
# which the api sets separately at mint time).
def _job_values(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec_name": job.get("spec_name", ""),
        "title": job.get("title", ""),
        "repo": job.get("repo", ""),
        "state": job.get("state", ""),
        "phase": job.get("phase", ""),
        "verify_ok": job.get("verify_ok"),
        "headline": job.get("headline") or [],
        "machine": job.get("machine") or {},
        "phase_times": job.get("phase_times") or [],
        "error": job.get("error") or "",
        "artifact_name": job.get("artifact_name", ""),
    }


class JlaunchRunRepo:
    """CRUD for the owner's run index on a caller-supplied RLS-scoped session."""

    async def upsert(self, session: AsyncSession, job: dict[str, Any]) -> None:
        """Insert or refresh a run row from a control-server job dict (all fields but the
        artifact pointer). Bumps `last_active_at`."""
        values = _job_values(job)
        stmt = (
            pg_insert(JlaunchRun)
            .values(id=job["id"], last_active_at=func.now(), **values)
            .on_conflict_do_update(
                index_elements=[JlaunchRun.id],
                set_={**values, "last_active_at": func.now()},
            )
        )
        await session.execute(stmt)

    async def set_artifact(
        self, session: AsyncSession, run_id: str, *, sha256: str, size: int, name: str
    ) -> None:
        """Record the collected artifact pointer (content-addressed sha + size + filename)
        for a run, at mint time."""
        await session.execute(
            pg_insert(JlaunchRun)
            .values(id=run_id, artifact_sha256=sha256, artifact_bytes=size, artifact_name=name)
            .on_conflict_do_update(
                index_elements=[JlaunchRun.id],
                set_={"artifact_sha256": sha256, "artifact_bytes": size, "artifact_name": name},
            )
        )

    async def list(self, session: AsyncSession) -> list[JlaunchRunRow]:
        result = await session.execute(
            select(JlaunchRun).order_by(JlaunchRun.last_active_at.desc())
        )
        return [JlaunchRunRow.of(r) for r in result.scalars()]

    async def get(self, session: AsyncSession, run_id: str) -> JlaunchRunRow | None:
        row = await session.get(JlaunchRun, run_id)
        return JlaunchRunRow.of(row) if row else None

    async def set_state(
        self, session: AsyncSession, run_id: str, *, state: str, phase: str
    ) -> None:
        """Reconcile only the live state/phase against the control server (the source of
        truth), leaving `last_active_at` untouched."""
        await session.execute(
            update(JlaunchRun).where(JlaunchRun.id == run_id).values(state=state, phase=phase)
        )

    async def delete(self, session: AsyncSession, run_id: str) -> None:
        await session.execute(delete(JlaunchRun).where(JlaunchRun.id == run_id))
