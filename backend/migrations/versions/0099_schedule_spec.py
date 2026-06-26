"""Give workflow schedules the task-style spec: kind/freq/days/time/run_at.

`app.schedules` was interval-only (a fixed forward step). To let the owner set a
sweep's cadence the way a task is scheduled — on-demand / once / daily / weekdays /
weekly at a wall-clock time — the table gains the same schedule-spec columns
`app.tasks` carries (migration 0093), and the next-fire computation reuses the same
pure `jbrain.tasks.schedule.next_run_after`.

Existing rows keep `schedule_kind='interval'` (the default), so the reconcilers and
any un-edited sweep fire exactly as before — sub-day cadences the task model can't
express stay interval-driven. A repeat/once/on_demand schedule leaves
`interval_seconds` NULL, so that column drops its NOT NULL; `next_run_at` becomes
nullable too (a spent one-off / an on-demand schedule has no next fire), and the due
index is narrowed to skip the NULLs.

Revision ID: 0099
Revises: 0098
Create Date: 2026-06-26
"""

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # interval_seconds is only meaningful for the legacy 'interval' kind; a
    # spec-driven schedule leaves it NULL. next_run_at is NULL for a schedule with
    # no upcoming fire (on_demand, or a once whose moment has passed).
    op.execute("ALTER TABLE app.schedules ALTER COLUMN interval_seconds DROP NOT NULL")
    op.execute("ALTER TABLE app.schedules ALTER COLUMN next_run_at DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE app.schedules
            ADD COLUMN schedule_kind text NOT NULL DEFAULT 'interval'
                CHECK (schedule_kind IN ('interval', 'on_demand', 'once', 'repeat')),
            ADD COLUMN schedule_freq text
                CHECK (schedule_freq IS NULL OR schedule_freq IN ('daily', 'weekdays', 'weekly')),
            ADD COLUMN schedule_days int[] NOT NULL DEFAULT '{}',
            ADD COLUMN schedule_time text,
            ADD COLUMN run_at timestamptz
        """
    )
    # The scheduler claims enabled, due rows; a NULL next_run_at is never due, so
    # narrow the partial index to match (and keep it covering only firable rows).
    op.execute("DROP INDEX IF EXISTS app.schedules_due_idx")
    op.execute(
        "CREATE INDEX schedules_due_idx ON app.schedules (next_run_at)"
        " WHERE enabled AND next_run_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.schedules_due_idx")
    op.execute(
        """
        ALTER TABLE app.schedules
            DROP COLUMN schedule_kind,
            DROP COLUMN schedule_freq,
            DROP COLUMN schedule_days,
            DROP COLUMN schedule_time,
            DROP COLUMN run_at
        """
    )
    op.execute("CREATE INDEX schedules_due_idx ON app.schedules (next_run_at) WHERE enabled")
    # Restore NOT NULL only if no spec-driven rows left a NULL behind (best-effort —
    # a clean downgrade assumes the spec feature was unused).
    op.execute("ALTER TABLE app.schedules ALTER COLUMN interval_seconds SET NOT NULL")
    op.execute("ALTER TABLE app.schedules ALTER COLUMN next_run_at SET NOT NULL")
