"""Minecraft play sessions: who was on the box's Bedrock server, and for how long.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M1 — the Ops screen's Players section (time
played, sessions, first and last seen). One row per session, written by the api's drain
of the sidecar's join/leave events (`jbrain/minecraft/sessions.py`).

**Keyed by xuid**, the Xbox account id, so a gamertag change doesn't split a player; the
gamertag is kept per row and the newest one is shown.

**Replay-safe.** `(boot_id, start_event_id)` is unique: the drain may re-read events
after an api restart and a join it already stored inserts nothing. `last_seen_at` is a
heartbeat on open rows, so a session cut off by a crash (no leave was ever logged)
closes at the last moment the player was seen, never at "now".

**Owner-only.** Who in the family plays when is the owner's to see, and there is no
scoped-token or family case for it, so the policy is `app.is_owner()` and nothing else
(CLAUDE.md #3: the firewall is Postgres, not the caller).
"""

from alembic import op

revision = "0224"
down_revision = "0223"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.mc_player_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            xuid text NOT NULL,
            gamertag text NOT NULL,
            boot_id text NOT NULL,
            start_event_id bigint NOT NULL,
            started_at timestamptz NOT NULL,
            last_seen_at timestamptz NOT NULL,
            ended_at timestamptz,
            UNIQUE (boot_id, start_event_id)
        )
        """
    )
    op.execute("CREATE INDEX mc_player_sessions_xuid_idx ON app.mc_player_sessions (xuid)")
    # The drain touches only open rows on every tick; keep that lookup tiny.
    op.execute(
        "CREATE INDEX mc_player_sessions_open_idx ON app.mc_player_sessions (xuid)"
        " WHERE ended_at IS NULL"
    )
    op.execute("ALTER TABLE app.mc_player_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_player_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mc_player_sessions_owner ON app.mc_player_sessions
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_player_sessions TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.mc_player_sessions")
