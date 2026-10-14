"""Each player's respawn point in each world, as the owner's map shows it (plan §M8, beds;
the binding mock docs/mocks/minecraft-map-layers/, beds variant B).

The game keeps a player's spawn point and tells nobody when it changes, so the pack reads
it (`getSpawnPoint()`) a tick after a bed or respawn anchor is used, on every join and
respawn, and every ten seconds, and sends a `spawnpoint` line when it changed. The session
drain keeps one row per change here — the history, not just the latest — so the map can say
where each death's player woke against the point they had then, and when the current one
was set.

- `kind` is what stood at the point when the pack last looked: `bed`, `anchor`, `other` (a
  point with no bed or anchor seen there: a /spawnpoint, or a bed broken since — Bedrock
  keeps the point until the next death), or `none` (no point: they wake at the world
  spawn), which has no place.
- Keyed for replay like the travel log's events: `(boot_id, event_id)` is unique, so
  re-reading the sidecar's events adds nothing; a line that repeats the player's latest row
  adds nothing either.
- A reset or import of the world clears its rows, as it does the travel log.

Owner-only, and deliberately NOT in the travel log (`app.mc_player_events`), which 0230
opens to map share links: the owner picked beds as owner-only, and the firewall belongs in
RLS (review X3). No share policy.

Revision ID: 0246
Revises: 0245
Create Date: 2026-10-13
"""

from alembic import op

revision = "0246"
down_revision = "0245"
branch_labels = None
depends_on = None

_DIMS = "('overworld', 'nether', 'the_end')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE app.mc_spawn_points (
            id bigserial PRIMARY KEY,
            world text NOT NULL,
            xuid text NOT NULL,
            gamertag text NOT NULL,
            boot_id text NOT NULL,
            event_id bigint NOT NULL,
            at timestamptz NOT NULL,
            kind text NOT NULL CHECK (kind IN ('bed', 'anchor', 'other', 'none')),
            dim text CHECK (dim IN {_DIMS}),
            x integer,
            y integer,
            z integer,
            UNIQUE (boot_id, event_id),
            CHECK ((kind = 'none') = (dim IS NULL)),
            CHECK (dim IS NULL OR (x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX mc_spawn_points_player ON app.mc_spawn_points (world, xuid, at)")
    op.execute("ALTER TABLE app.mc_spawn_points ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_spawn_points FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY mc_spawn_points_owner ON app.mc_spawn_points"
        " USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_spawn_points TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.mc_spawn_points_id_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.mc_spawn_points")
