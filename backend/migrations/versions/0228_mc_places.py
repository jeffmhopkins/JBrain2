"""Minecraft: each player's saved places, and the in-game door's sessions.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M9/§P2. The owner's target: "Dave, mark this
location as tree farm", then "Dave, navigate to tree farm".

- **`mc_places`**: a player's named places in one world ("home", "tree farm"), keyed like
  the other mc_* tables by xuid — or `name:<gamertag>` for a player the server hasn't seen
  yet. A name is unique per player and world, ignoring case, so saving "Tree Farm" again
  moves the place rather than adding a second one. A world folder, not a slot, because a
  slot's folder is what its terrain lives in (the travel log keys the same way).
- **`mc_chat_player.door`**: true on the Minecraft_Dave session the in-game door
  (`/jb:dave`) opened for a player, so their next question continues it. A PWA chat about
  the same player is not that player's in-game conversation.

Owner-only like 0226 (`app.is_owner()`), with an isolation test (CLAUDE.md #3): players
are never database principals; the door writes through the api's owner context on behalf
of the xuid the server vouched for.
"""

from alembic import op

revision = "0228"
down_revision = "0227"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.mc_places (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            xuid text NOT NULL,
            gamertag text NOT NULL,
            world text NOT NULL,
            name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
            x integer NOT NULL,
            y integer,
            z integer NOT NULL,
            dim text NOT NULL CHECK (dim IN ('overworld', 'nether', 'the_end')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX mc_places_name_idx ON app.mc_places (xuid, world, lower(name))")
    op.execute("ALTER TABLE app.mc_places ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_places FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY mc_places_owner ON app.mc_places"
        " USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_places TO jbrain_app")
    op.execute("ALTER TABLE app.mc_chat_player ADD COLUMN door boolean NOT NULL DEFAULT false")
    op.execute("CREATE INDEX mc_chat_player_door_idx ON app.mc_chat_player (xuid) WHERE door")


def downgrade() -> None:
    op.execute("DROP INDEX app.mc_chat_player_door_idx")
    op.execute("ALTER TABLE app.mc_chat_player DROP COLUMN door")
    op.execute("DROP TABLE app.mc_places")
