"""GrowBot robots get a name, a revoked state and a last-seen time (wave G2).

docs/plans/GROWBOT_PLAN.md §3. The PWA flashes and names a robot, shows when each was last
on the relay, and revokes a key without forgetting the robot (G1's DELETE forgets it):

- `name` — what the owner called it, up to 40 characters ('' for a G1 row).
- `revoked_at` — when its key was revoked. A revoked robot's `key_hash` is NULL, so no key
  can ever match it; a reflash mints a new key and clears this.
- `last_seen_at` — stamped each time the relay verifies the robot's hello, which is how the
  flash knows the robot reached the box.

`key_hash` becomes nullable for that revoke (the CHECK still holds any value that is set).
RLS is unchanged: the table stays owner-only (`growbot_robots_owner`).

Revision ID: 0255
Revises: 0254
Create Date: 2026-11-10
"""

from alembic import op

revision = "0255"
down_revision = "0254"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.growbot_robots
            ADD COLUMN name text NOT NULL DEFAULT '' CHECK (char_length(name) <= 40),
            ADD COLUMN revoked_at timestamptz,
            ADD COLUMN last_seen_at timestamptz,
            ALTER COLUMN key_hash DROP NOT NULL
        """
    )


def downgrade() -> None:
    # A revoked robot has no key to restore; it is forgotten, as G1 would have done.
    op.execute("DELETE FROM app.growbot_robots WHERE key_hash IS NULL")
    op.execute(
        """
        ALTER TABLE app.growbot_robots
            ALTER COLUMN key_hash SET NOT NULL,
            DROP COLUMN last_seen_at,
            DROP COLUMN revoked_at,
            DROP COLUMN name
        """
    )
