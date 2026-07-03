"""Add a nullable `skill_version` column to `app.runs` (Track N, N2).

The deferred Track-C auditability item (docs/reference/ASSISTANT.md): so a Phase-6
skill-promoted run is auditable — stamped with the version of the skill it ran
under — without a later schema change. This is groundwork only: the column is
nullable and NO logic writes it yet (the skills table has no consumer this phase).

Column-only, mirroring `prompt_version` next to it; no new table, so no new RLS
isolation test — `app.runs` keeps its owner-only posture (the existing runs RLS
test re-asserts green). Reversible: up adds, down drops.

Re-chained at integration to follow 0042 (Track S's reconciler seed) so the
migration chain stays linear-single-head after both tracks merged.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-16
"""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.runs ADD COLUMN skill_version text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.runs DROP COLUMN skill_version")
