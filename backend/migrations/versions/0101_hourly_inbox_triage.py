"""Run the inbox-triage sweep hourly, and turn it on.

0096 seeded the `triage_inbox` schedule daily and **disabled** (the owner fired it
from Ops by hand while working the backlog down). The inbox is caught up now, so the
sweep should run on its own: switch its interval to hourly and enable it. `next_run_at`
is reset to fire on the next scheduler tick rather than waiting out the old daily slot.

The schedule stays `schedule_kind='interval'` (sub-day cadence the task model can't
express). Targets the fixed schedule id 0096 minted so it's addressable across envs.

Revision ID: 0101
Revises: 0100
Create Date: 2026-06-26
"""

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

_SCHEDULE_ID = "00000000-0000-0000-0000-000000100001"
_PIPELINE = "daily_inbox_triage"  # the seeded identifier (0096); kept stable, not renamed
_HOURLY = 3600
_DAILY = 86400
# The behavior changed (whole inbox, high kept in place), so refresh the Ops-facing
# pipeline description to match. The name stays as seeded — the trigger references it.
_DESC = "Classify untriaged inbox mail into triaged/* labels; archive all but high."
_OLD_DESC = "Classify the newest day of inbox mail into triaged/* labels and archive it."


def upgrade() -> None:
    op.execute(
        "UPDATE app.schedules"
        f" SET interval_seconds = {_HOURLY}, enabled = true, next_run_at = now()"
        f" WHERE id = '{_SCHEDULE_ID}'"
    )
    op.execute(
        f"UPDATE app.pipelines SET description = '{_DESC}'"
        f" WHERE name = '{_PIPELINE}' AND version = 1"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app.schedules"
        f" SET interval_seconds = {_DAILY}, enabled = false"
        f" WHERE id = '{_SCHEDULE_ID}'"
    )
    op.execute(
        f"UPDATE app.pipelines SET description = '{_OLD_DESC}'"
        f" WHERE name = '{_PIPELINE}' AND version = 1"
    )
