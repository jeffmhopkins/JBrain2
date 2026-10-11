"""Public share links for the Minecraft map: a revocable, no-login view of one world.

The owner picks a world slot and a gamertag on the map and gets a link; anyone with it
sees that world's map opened on that player, with every player's live position, travel
history and timeline, and nothing else on the box. A plain token-in-URL bearer read: only
the token's SHA-256 is stored, there is no expiry, and `revoked_at` kills it. Mirrors the
research (0150) and jlaunch (0153) shares.

The capability is enforced in RLS. A visitor runs under `mc_share_context`, a non-owner,
empty-scope context carrying `auth_context = 'mc_share'` and, once the token resolves,
`principal_id = <link id>`. The `*_share` policies below are the ONLY grants it earns: each
joins the pinned, unrevoked link to the row's world folder, so a link reads its own world's
travel log and no other. Sessions carry no world; one belongs to the world its join event
was recorded in (the same rule `minecraft/timeline.py` reads them by). The link id is
compared as text, never cast, so an empty pin fails closed instead of throwing.

Revision ID: 0230
Revises: 0229
Create Date: 2026-10-11
"""

from alembic import op

revision = "0230"
down_revision = "0229"
branch_labels = None
depends_on = None

# The travel-log tables keyed by world folder.
_WORLD_TABLES = ("mc_player_track", "mc_player_explored", "mc_player_events")

_LINK_LIVE = """
    l.id::text = current_setting('app.principal_id', true)
    AND l.revoked_at IS NULL
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.mc_share_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Only the SHA-256 of the 256-bit bearer secret; the secret is shown once at
            -- mint and never recoverable.
            token_hash text NOT NULL UNIQUE,
            slot text NOT NULL,
            -- The slot's world folder at mint: what the travel log is keyed by.
            world text NOT NULL,
            -- The player the map opens on (the travel log's player key) and their tag.
            xuid text NOT NULL,
            gamertag text NOT NULL,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            last_viewed_at timestamptz,
            view_count integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("ALTER TABLE app.mc_share_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_share_links FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mc_share_links_owner ON app.mc_share_links
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY mc_share_links_public_read ON app.mc_share_links
        FOR SELECT USING (app.auth_ctx() = 'mc_share')
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_share_links TO jbrain_app")

    for table in _WORLD_TABLES:
        op.execute(
            f"""
            CREATE POLICY {table}_share ON app.{table} FOR SELECT
            USING (
                app.auth_ctx() = 'mc_share'
                AND EXISTS (
                    SELECT 1 FROM app.mc_share_links l
                    WHERE {_LINK_LIVE} AND l.world = app.{table}.world
                )
            )
            """
        )
    op.execute(
        f"""
        CREATE POLICY mc_player_sessions_share ON app.mc_player_sessions FOR SELECT
        USING (
            app.auth_ctx() = 'mc_share'
            AND EXISTS (
                SELECT 1 FROM app.mc_share_links l
                JOIN app.mc_player_events j ON j.world = l.world
                WHERE {_LINK_LIVE}
                  AND j.kind = 'join'
                  AND j.boot_id = app.mc_player_sessions.boot_id
                  AND j.event_id = app.mc_player_sessions.start_event_id
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY mc_player_sessions_share ON app.mc_player_sessions")
    for table in _WORLD_TABLES:
        op.execute(f"DROP POLICY {table}_share ON app.{table}")
    op.execute("DROP TABLE app.mc_share_links")
