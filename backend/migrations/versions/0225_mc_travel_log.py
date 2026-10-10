"""Minecraft travel log: where each player went, what they uncovered, and what happened.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §T1, recorded from early on so M8's maps and M8a's
timeline have history to draw when they arrive. All three tables are filled by the api's
drains of the sidecar (`jbrain/minecraft/travel.py`, `jbrain/minecraft/sessions.py`).

- **`mc_player_track`**: one row per kept position sample (the sidecar drops samples
  under a few blocks of movement). Replay-safe on `(boot_id, sample_id)`.
- **`mc_player_explored`**: per-player fog of war, one row per chunk a player came
  within a few chunks of, with when it was first and last seen. Upserts widen the
  first/last window, so replaying samples never narrows it.
- **`mc_player_events`**: the timeline's markers — joined, left, died (with its cause),
  respawned — plus `world_replaced`, the marker a reset or import leaves. Replay-safe
  on `(boot_id, event_id)`.

**Per world folder.** Rows carry the slot folder the server was running. A reset or an
import replaces that folder's terrain, so its trail, fog and events are deleted then;
the `world_replaced` marker stays, and stops a late-draining sample of the old terrain
from coming back.

**Owner-only**, like play sessions (0224): where the family went is the owner's to see,
enforced by `app.is_owner()` in Postgres (CLAUDE.md #3).
"""

from alembic import op

revision = "0225"
down_revision = "0224"
branch_labels = None
depends_on = None

_TABLES = ("mc_player_track", "mc_player_explored", "mc_player_events")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.mc_player_track (
            id bigserial PRIMARY KEY,
            world text NOT NULL,
            xuid text NOT NULL,
            gamertag text NOT NULL,
            boot_id text NOT NULL,
            sample_id bigint NOT NULL,
            at timestamptz NOT NULL,
            dim text NOT NULL,
            x double precision NOT NULL,
            y double precision NOT NULL,
            z double precision NOT NULL,
            yaw double precision,
            UNIQUE (boot_id, sample_id)
        )
        """
    )
    # The timeline reads one world's trails over a time range, per player.
    op.execute("CREATE INDEX mc_player_track_world_at_idx ON app.mc_player_track (world, at)")
    op.execute(
        """
        CREATE TABLE app.mc_player_explored (
            world text NOT NULL,
            xuid text NOT NULL,
            dim text NOT NULL,
            cx integer NOT NULL,
            cz integer NOT NULL,
            first_seen timestamptz NOT NULL,
            last_seen timestamptz NOT NULL,
            PRIMARY KEY (world, xuid, dim, cx, cz)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.mc_player_events (
            id bigserial PRIMARY KEY,
            world text NOT NULL,
            xuid text NOT NULL,
            gamertag text NOT NULL,
            boot_id text NOT NULL,
            event_id bigint NOT NULL,
            at timestamptz NOT NULL,
            kind text NOT NULL,
            dim text,
            x double precision,
            y double precision,
            z double precision,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (boot_id, event_id)
        )
        """
    )
    op.execute("CREATE INDEX mc_player_events_world_at_idx ON app.mc_player_events (world, at)")
    for table in _TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table}"
            " USING (app.is_owner()) WITH CHECK (app.is_owner())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")
    for seq in ("mc_player_track_id_seq", "mc_player_events_id_seq"):
        op.execute(f"GRANT USAGE ON SEQUENCE app.{seq} TO jbrain_app")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE app.{table}")
