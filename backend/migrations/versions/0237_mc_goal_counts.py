"""Goals that count: "I need 23 iron blocks" (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M24).

A Minecraft_Dave goal can now name an item and how many of it the player needs, so Dave
can say how far along they are: what they carry plus what's in the chests near their own
saved places. Both columns are nullable — every goal written before this has neither and
reads exactly as it did — and they come as a pair: a count with no item (or an item with
no count) means nothing to count.

`item` is a Bedrock item id as `mc_find_item` takes it (namespace optional, the
`minecraft:` one dropped), so the CHECK repeats the tool's own pattern. No new table and
no policy change: `app.mc_goals` stays owner-only (0226), and its isolation test now
writes the new columns too.

Revision ID: 0237
Revises: 0236
Create Date: 2026-10-12
"""

from alembic import op

revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.mc_goals
            ADD COLUMN item text
                CHECK (item ~ '^([a-z0-9_]{1,32}:)?[a-z0-9_]{2,64}$'),
            ADD COLUMN item_count integer CHECK (item_count BETWEEN 1 AND 100000),
            ADD CONSTRAINT mc_goals_item_pair CHECK ((item IS NULL) = (item_count IS NULL))
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.mc_goals DROP CONSTRAINT mc_goals_item_pair,"
        " DROP COLUMN item_count, DROP COLUMN item"
    )
