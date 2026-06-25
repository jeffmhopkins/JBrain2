"""Add a live progress note to runs (workflow-engine observability).

A long-running job (e.g. inbox triage) is a single run-log step that stays opaque
until it finalizes. `runs.progress_note` is a free-text line the handler updates as it
works ("processed 15 of 30 emails"), which the Ops "Runs" screen already polls every
few seconds while a run is in flight — so the owner can watch progress instead of a
bare "running". Counts/phase only, never content. Cleared when the run closes.

Nullable text, no backfill (existing runs simply have no note). No new table, so no
RLS policy change — the column rides the runs table's owner-only RLS.

Revision ID: 0097
Revises: 0096
Create Date: 2026-06-25
"""

from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.runs ADD COLUMN progress_note text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.runs DROP COLUMN progress_note")
