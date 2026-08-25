"""jmolt's weekly observability metrics (docs/plans/JMOLT_PLAN.md, §5 W4).

Computes the observability rubric from what jmolt leaves behind — its nights, its action
ledger, its scratchpad, and its outbox — so the owner can see how the experiment is
actually going week to week (is a relationship graph forming? is the scratchpad alive?
is it participating on its own terms or drifting toward spam?). Read-only, under a
non-jmolt owner context; the numbers are computed here and rendered by `format_report`,
and the `scripts/jmolt-metrics.py` CLI prints them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.settings_store import SqlSettingsStore

_SYSTEM_OWNER = SessionContext(principal_kind="owner")


@dataclass(frozen=True)
class JmoltWeekMetrics:
    days: int
    nights_run: int
    nights_ok: int
    nights_error: int
    actions_by_type: dict[str, int] = field(default_factory=dict)
    distinct_targets: int = 0
    scratch_files: int = 0
    scratch_bytes: int = 0
    scratch_files_changed: int = 0
    outbox_by_status: dict[str, int] = field(default_factory=dict)
    account_state: str = "ok"
    verify_fail_streak: int = 0

    @property
    def actions_total(self) -> int:
        return sum(self.actions_by_type.values())


def _admin_ctx(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    async with scoped_session(maker, _SYSTEM_OWNER) as s:
        pid = (
            await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1"))
        ).scalar()
    return str(pid) if pid is not None else None


class JmoltMetrics:
    def __init__(
        self, *, maker: async_sessionmaker[AsyncSession], settings_store: SqlSettingsStore
    ) -> None:
        self._maker = maker
        self._settings = settings_store

    async def compute(self, *, now: datetime | None = None, days: int = 7) -> JmoltWeekMetrics:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=days)
        pid = await _owner_principal_id(self._maker)
        if pid is None:
            return JmoltWeekMetrics(days=days, nights_run=0, nights_ok=0, nights_error=0)
        admin = _admin_ctx(pid)

        async with scoped_session(self._maker, admin) as s:
            nights = (
                await s.execute(
                    text(
                        "SELECT r.status, count(*) AS n FROM app.agent_sessions se"
                        " LEFT JOIN app.runs r ON r.session_id = se.id AND r.kind = 'agent'"
                        " WHERE se.agent = 'jmolt' AND se.created_at >= :cut GROUP BY r.status"
                    ),
                    {"cut": cutoff},
                )
            ).all()
            actions = (
                await s.execute(
                    text(
                        "SELECT action, count(*) AS n FROM app.jmolt_action_ledger"
                        " WHERE principal_id = :pid AND at >= :cut GROUP BY action"
                    ),
                    {"pid": pid, "cut": cutoff},
                )
            ).all()
            distinct_targets = (
                await s.execute(
                    text(
                        "SELECT count(DISTINCT target) FROM app.jmolt_action_ledger"
                        " WHERE principal_id = :pid AND at >= :cut AND coalesce(target,'') <> ''"
                    ),
                    {"pid": pid, "cut": cutoff},
                )
            ).scalar() or 0
            scratch = (
                await s.execute(
                    text(
                        "SELECT count(*) AS files, coalesce(sum(bytes),0) AS total"
                        " FROM app.jmolt_scratch WHERE principal_id = :pid"
                    ),
                    {"pid": pid},
                )
            ).one()
            files_changed = (
                await s.execute(
                    text(
                        "SELECT count(DISTINCT filename) FROM app.jmolt_scratch_archive"
                        " WHERE principal_id = :pid AND archived_at >= :cut"
                    ),
                    {"pid": pid, "cut": cutoff},
                )
            ).scalar() or 0
            outbox = (
                await s.execute(
                    text(
                        "SELECT status, count(*) AS n FROM app.jmolt_outbox"
                        " WHERE principal_id = :pid AND created_at >= :cut GROUP BY status"
                    ),
                    {"pid": pid, "cut": cutoff},
                )
            ).all()

        nights_ok = sum(r.n for r in nights if r.status == "done")
        nights_error = sum(r.n for r in nights if r.status not in ("done", None))
        return JmoltWeekMetrics(
            days=days,
            nights_run=sum(r.n for r in nights),
            nights_ok=nights_ok,
            nights_error=nights_error,
            actions_by_type={r.action: r.n for r in actions},
            distinct_targets=int(distinct_targets),
            scratch_files=int(scratch.files),
            scratch_bytes=int(scratch.total),
            scratch_files_changed=int(files_changed),
            outbox_by_status={r.status: r.n for r in outbox},
            account_state=await self._settings.moltbook_account_state(admin),
            verify_fail_streak=await self._settings.moltbook_verify_fail_streak(admin),
        )


def format_report(m: JmoltWeekMetrics) -> str:
    """A plain-text weekly report from the metrics (what the CLI prints)."""
    lines = [
        f"jmolt — last {m.days} days",
        "",
        f"Nights: {m.nights_run} run ({m.nights_ok} ok, {m.nights_error} error)",
        f"Actions: {m.actions_total} total across {m.distinct_targets} distinct targets",
    ]
    for action, n in sorted(m.actions_by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  · {action}: {n}")
    lines.append(
        f"Scratchpad: {m.scratch_files} files, {m.scratch_bytes} bytes, "
        f"{m.scratch_files_changed} changed this week"
    )
    if m.outbox_by_status:
        parts = ", ".join(f"{k} {v}" for k, v in sorted(m.outbox_by_status.items()))
        lines.append(f"Outbox: {parts}")
    lines.append(f"Account state: {m.account_state} (verify-fail streak {m.verify_fail_streak})")
    return "\n".join(lines)
