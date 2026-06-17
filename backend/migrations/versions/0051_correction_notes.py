"""Owner correction notes (Phase 6 Wave A+): provenance + revision anchor.

The correction loop lets the owner "out-argue the wiki" with an owner-authored note whose facts
extract at full weight and force-supersede + pin the current head (see analysis/supersession and
arbiter). Two schema additions support it:
- `notes.provenance` gains the `owner_correction` value (the CHECK from 0018 allowed only
  human/agent); the pipeline reads it to elevate + force-supersede.
- `notes.wiki_revision_id` anchors a correction to the wiki revision it disputes (the Talk/
  correction UI sets it; nullable, ON DELETE SET NULL so a rebuilt revision doesn't orphan-delete
  the correction note — the override fact stands on its own).

Revision ID: 0051
Revises: 0050
Create Date: 2026-06-17
"""

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP CONSTRAINT IF EXISTS notes_provenance_check")
    op.execute(
        "ALTER TABLE app.notes ADD CONSTRAINT notes_provenance_check"
        " CHECK (provenance IN ('human', 'agent', 'owner_correction'))"
    )
    op.execute(
        "ALTER TABLE app.notes ADD COLUMN wiki_revision_id uuid"
        " REFERENCES app.wiki_revisions(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP COLUMN wiki_revision_id")
    op.execute("ALTER TABLE app.notes DROP CONSTRAINT IF EXISTS notes_provenance_check")
    # Re-adding the stricter CHECK validates existing rows, so fold any owner_correction notes
    # back to 'human' first (else the downgrade would abort on a live correction note).
    op.execute("UPDATE app.notes SET provenance = 'human' WHERE provenance = 'owner_correction'")
    op.execute(
        "ALTER TABLE app.notes ADD CONSTRAINT notes_provenance_check"
        " CHECK (provenance IN ('human', 'agent'))"
    )
