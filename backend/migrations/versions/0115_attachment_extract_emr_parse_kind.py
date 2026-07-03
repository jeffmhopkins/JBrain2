"""Admit the 'emr_parse' attachment-extract kind (EMR import Wave 2).

The EMR importer may cache a decrypted attachment's deterministic per-source
parser output as an app.attachment_extracts row of kind='emr_parse' (a
re-runnable stage; docs/plans/EMR_IMPORT_PLAN.md §6.3). The `kind` allowlist is
an explicit CHECK (0011, widened by 0079/0084), so the new kind is admitted here.

Verified current set (through 0084) is the FOUR-value
('ocr','caption','transcript','video_analysis'); `up` adds 'emr_parse' to it and
`down` restores that four-value set after deleting the new rows — it must NOT
narrow to the 0079 three-value set (that would drop 'video_analysis' and break
the next video ingest). Rides the existing RLS policy + grants (no new table, no
new isolation test). The pyzipper AES-zip dependency ships in the same PR
(scripts/dev-setup.sh + backend/pyproject.toml, invariant #8).

Revision ID: 0115
Revises: 0114
Create Date: 2026-07-03
"""

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None

_KINDS_WITH = "('ocr', 'caption', 'transcript', 'video_analysis', 'emr_parse')"
_KINDS_WITHOUT = "('ocr', 'caption', 'transcript', 'video_analysis')"


def upgrade() -> None:
    op.execute("ALTER TABLE app.attachment_extracts DROP CONSTRAINT attachment_extracts_kind_check")
    op.execute(
        "ALTER TABLE app.attachment_extracts ADD CONSTRAINT attachment_extracts_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    # Clear the new kind before narrowing, or the re-add would fail. Restores the
    # 0084 FOUR-value set — never the 0079 three-value set.
    op.execute("DELETE FROM app.attachment_extracts WHERE kind = 'emr_parse'")
    op.execute("ALTER TABLE app.attachment_extracts DROP CONSTRAINT attachment_extracts_kind_check")
    op.execute(
        "ALTER TABLE app.attachment_extracts ADD CONSTRAINT attachment_extracts_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
