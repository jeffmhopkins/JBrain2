"""Optional expiry (TTL) for research-library reports.

Reports live forever by default (right for a candidate profile you'll revisit), but a
`daily_news` briefing (REPORT_PRESET_PLAN.md) is a throwaway — you hear it on the drive in
and want it gone within a week. This adds an opt-in `expires_at`: NULL = keep forever (every
existing row and every non-retention run, untouched — no backfill), a timestamp = eligible
for the nightly `expire_research_reports` sweep, which hard-deletes rows past their instant
(REPORT_EXPIRY_PLAN.md, W1). `persist_report` stamps it from a preset's `retention_days`.

Additive + nullable, so no backfill. A partial index over the non-NULL rows keeps the
sweep's due-scan (`WHERE expires_at IS NOT NULL AND expires_at < now()`) off a full scan
even as the library grows, while costing nothing for the common keep-forever row.
"""

from alembic import op

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.research_reports ADD COLUMN expires_at timestamptz")
    op.execute(
        "CREATE INDEX ix_research_reports_expires_at ON app.research_reports (expires_at)"
        " WHERE expires_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_research_reports_expires_at")
    op.execute("ALTER TABLE app.research_reports DROP COLUMN expires_at")
