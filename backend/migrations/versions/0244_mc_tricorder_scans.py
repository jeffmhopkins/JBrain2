"""The Tricorder's live scan each player has on, as the map and Dave see it (plan §M25,
the binding mock docs/mocks/minecraft-scan-on-map/, variant A with C's depth line).

The scan lives in the world, as the pack's `jb:scan:<gamertag>` dynamic property, and
what it finds lives only in the pack's memory. The pack now sends a structured line when a
scan starts, when what it finds changes (and once a minute while it doesn't), and when it
stops, and every kept scan after each load; the session drain keeps this table in step.
A sibling of `app.mc_tricorder_targets`, not columns on it: the waypoint under a scan is
kept and comes back when the scan stops, so the two must never overwrite each other.

- Keyed by the casefolded gamertag, the pack's own key, like the targets.
- `state` is `set` (on, nothing reported yet), `found` (with the block in `dim, x, y, z`)
  or `none` (nothing within reach). `scanned_at` is the last scan heard, `set_at` when the
  drain heard it start (NULL when only a snapshot named it).
- A reset or import of the world clears it, as it does the beacons and gates.

Owner-only, plus a SELECT for a map share link pinned to the row's world: shared maps show
the scan by the Tricorder target's rule (0232).

Revision ID: 0244
Revises: 0243
Create Date: 2026-10-13
"""

from alembic import op

revision = "0244"
down_revision = "0243"
branch_labels = None
depends_on = None

_DIMS = "('overworld', 'nether', 'the_end')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE app.mc_tricorder_scans (
            world text NOT NULL,
            player text NOT NULL,
            gamertag text NOT NULL,
            label text NOT NULL,
            ids text[] NOT NULL DEFAULT '{{}}',
            state text NOT NULL CHECK (state IN ('set', 'found', 'none')),
            dim text CHECK (dim IN {_DIMS}),
            x integer,
            y integer,
            z integer,
            set_at timestamptz,
            scanned_at timestamptz,
            seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (world, player),
            CHECK ((state = 'found') = (dim IS NOT NULL AND x IS NOT NULL
                                         AND y IS NOT NULL AND z IS NOT NULL))
        )
        """
    )
    op.execute("ALTER TABLE app.mc_tricorder_scans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_tricorder_scans FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mc_tricorder_scans_owner ON app.mc_tricorder_scans
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY mc_tricorder_scans_share ON app.mc_tricorder_scans FOR SELECT
        USING (
            app.auth_ctx() = 'mc_share'
            AND EXISTS (
                SELECT 1 FROM app.mc_share_links l
                WHERE l.id::text = current_setting('app.principal_id', true)
                  AND l.revoked_at IS NULL
                  AND l.world = app.mc_tricorder_scans.world
            )
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_tricorder_scans TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.mc_tricorder_scans")
