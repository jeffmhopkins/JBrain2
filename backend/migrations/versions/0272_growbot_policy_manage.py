"""GrowBot's policy library: the owner's switches for each policy.

The owner asked (2026-11-14) to manage the Body tab's policies, not only rename and delete
them: turn one off without deleting it, say what kind of motion it is, and pick which one of
a family the pad uses.

- `enabled` — offered on the Body tab's pad; off keeps it in the library, unused.
- `motion` — what the pad does with it: `walk` drives the pad's four keys, `turn` its left
  and right keys, `trick` is a trick button that holds it facing ahead.
- `active` — the one of its family (`source_spec`, the training job it came from) and motion
  the pad offers; at most one per family and motion, which the partial unique index holds, so
  picking a family's turn keeps its walk's pick. With none active the pad offers the newest
  enabled one.

The table's owner-only RLS (0268) is unchanged.

Revision ID: 0272
Revises: 0271
Create Date: 2026-11-14
"""

from alembic import op

revision = "0272"
down_revision = "0271"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.growbot_policies
            ADD COLUMN enabled boolean NOT NULL DEFAULT true,
            ADD COLUMN motion text NOT NULL DEFAULT 'walk'
                CHECK (motion IN ('walk', 'turn', 'trick')),
            ADD COLUMN active boolean NOT NULL DEFAULT false
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX growbot_policies_one_active"
        " ON app.growbot_policies (source_spec, motion) WHERE active"
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.growbot_policies_one_active")
    op.execute(
        "ALTER TABLE app.growbot_policies"
        " DROP COLUMN active, DROP COLUMN motion, DROP COLUMN enabled"
    )
