"""A claim stamp so a deferred analysis auto-resumes into the chat exactly once.

The finished off-turn analysis (`analyze_stream` full mode) is fed back into the chat as
a `deferred_outcome` turn carrying its summary + transcript excerpt — the step that lets
jerv answer a later "read the full transcript" from context. That injection used to fire
only from the `task_status` card's live running→done transition, so a job that finished
while the PWA wasn't watching the card (a backgrounded/killed tab that reopens minutes
later, already-done) NEVER auto-resumed: the owner saw the transcript card, the model
never did, and a follow-up wrongly reported "no transcript".

This adds the atomic claim the reliable-resume fix keys off: the card fires the resume on
`done` regardless of whether it witnessed the transition, and a single `resumed_at`
claim (this column, set once) keeps it exactly-once across reloads, extra tabs, and a
mount-already-done. Nullable, owner-only like the rest of the row (migration 0132), no
firewall column.

Revision ID: 0138
Revises: 0137
Create Date: 2026-07-19
"""

from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # When the finished analysis was claimed for its one auto-resume turn. Null = not yet
    # resumed; the claim UPDATE sets it once (guarded `WHERE resumed_at IS NULL`), so only
    # the first claimant sends the follow-up.
    op.execute("ALTER TABLE app.media_analysis_results ADD COLUMN resumed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE app.media_analysis_results DROP COLUMN IF EXISTS resumed_at")
