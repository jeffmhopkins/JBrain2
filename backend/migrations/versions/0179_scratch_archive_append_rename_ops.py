"""Let the scratch archive record the two new write kinds (append, rename).

`jmolt_scratch_archive.op` is CHECK-constrained to ('write', 'delete') — the only two
things the scratchpad could do when 0173 was written. H1 adds append and rename
(`docs/plans/JMOLT_HARDENING_PLAN.md`, E4), and every scratchpad change snapshots to the
archive, so without this the archive INSERT raises and the whole write fails.

The constraint is worth keeping rather than dropping: the archive is jmolt's only recovery
net and the `op` column is what a reader uses to tell a deliberate delete from a rewrite,
so an unconstrained free-text column would quietly degrade into noise.

Revision ID: 0179
Revises: 0178
Create Date: 2026-08-27
"""

from alembic import op

revision = "0179"
down_revision = "0178"
branch_labels = None
depends_on = None

_OPS = "'write', 'append', 'rename', 'delete'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.jmolt_scratch_archive DROP CONSTRAINT jmolt_scratch_archive_op_check"
    )
    op.execute(
        f"ALTER TABLE app.jmolt_scratch_archive"
        f" ADD CONSTRAINT jmolt_scratch_archive_op_check CHECK (op IN ({_OPS}))"
    )


def downgrade() -> None:
    # Rows recording the new ops would fail the old constraint, so fold them into the
    # closest thing it allows: both append and rename leave a full snapshot of the file,
    # exactly as a write does.
    op.execute("UPDATE app.jmolt_scratch_archive SET op = 'write' WHERE op IN ('append', 'rename')")
    op.execute(
        "ALTER TABLE app.jmolt_scratch_archive DROP CONSTRAINT jmolt_scratch_archive_op_check"
    )
    op.execute(
        "ALTER TABLE app.jmolt_scratch_archive"
        " ADD CONSTRAINT jmolt_scratch_archive_op_check CHECK (op IN ('write', 'delete'))"
    )
