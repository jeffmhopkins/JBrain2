"""Record which source mode produced a deep-research report.

`deep_research` gained a `sources` knob (`web` | `library` | `library_first` —
DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md); the report library should remember which mode a
stored report came from so a browse/recall can show "researched against your video
library" rather than assuming the web. Additive + nullable: an existing row (persisted
before this column) reads NULL, which the app treats as the legacy `web` default, so no
backfill is needed. No CHECK — the writer is the only producer and validates the enum in
code (a future fourth mode should not need a migration to store).
"""

from alembic import op

revision = "0142"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.research_reports ADD COLUMN source_mode text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.research_reports DROP COLUMN source_mode")
