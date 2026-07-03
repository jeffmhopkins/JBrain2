"""jcode_sessions: a title and an archive flag for the launcher's session manager.

The launcher gains jerv-style session management — rename, archive/unarchive, delete
(docs/reference/DESIGN.md "jcode"). `title` is the owner's optional label (repo name shows when
blank); `archived` is a separate boolean rather than a `status` value because `status`
holds the runtime state (ready/running) that every turn writes back — overloading it
would let a turn clobber the archived flag.
"""

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.jcode_sessions ADD COLUMN title text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE app.jcode_sessions ADD COLUMN archived boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE app.jcode_sessions DROP COLUMN IF EXISTS archived")
    op.execute("ALTER TABLE app.jcode_sessions DROP COLUMN IF EXISTS title")
