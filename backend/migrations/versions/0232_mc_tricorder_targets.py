"""The Tricorder target each player is following, as the map shows it (plan §M21).

Dave locks a player's Tricorder on a spot by writing a world dynamic property in game,
which nothing outside the world can read back. `mc_set_waypoint` now also keeps the
current target here, one row per player per world, so the map can draw it with a bearing
line from the player; the track drain deletes the row (and clears the Tricorder) once the
player reaches it. Keyed by the casefolded gamertag, the same key the pack uses: a target
can be set for a gamertag that has never been sampled.

Owner-only, plus a SELECT for a map share link pinned to the row's world (the same rule
as 0230's travel-log policies), so a shared map shows its players' targets too.

Revision ID: 0232
Revises: 0231
Create Date: 2026-10-12
"""

from alembic import op

revision = "0232"
down_revision = "0231"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.mc_tricorder_targets (
            world text NOT NULL,
            player text NOT NULL,
            gamertag text NOT NULL,
            dim text NOT NULL,
            x integer NOT NULL,
            y integer,
            z integer NOT NULL,
            label text NOT NULL,
            set_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (world, player)
        )
        """
    )
    op.execute("ALTER TABLE app.mc_tricorder_targets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_tricorder_targets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mc_tricorder_targets_owner ON app.mc_tricorder_targets
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY mc_tricorder_targets_share ON app.mc_tricorder_targets FOR SELECT
        USING (
            app.auth_ctx() = 'mc_share'
            AND EXISTS (
                SELECT 1 FROM app.mc_share_links l
                WHERE l.id::text = current_setting('app.principal_id', true)
                  AND l.revoked_at IS NULL
                  AND l.world = app.mc_tricorder_targets.world
            )
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_tricorder_targets TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.mc_tricorder_targets")
