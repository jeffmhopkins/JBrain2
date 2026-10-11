"""Minecraft: the deep-scan limit's count, per player per world per Minecraft day.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M17. In game each player gets two deep scans
(`mc_locate`, `mc_nearby`, `mc_find_block`) per Minecraft day; asking again for what was
already found today is free.

- **`mc_scans`**: one row per world folder, player key (xuid, or `name:<gamertag>`) and
  Minecraft day: scans used, and today's results (`[{tool, what, dim, x, z, text}]`) that
  answer a repeat. Only the current day matters, so writing a day deletes the world's
  other days (`minecraft/scans.py`).

Owner-only like 0228 (`app.is_owner()`), with an isolation test (CLAUDE.md #3): players
are never database principals; the door writes through the api's owner context.
"""

from alembic import op

revision = "0229"
down_revision = "0228"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.mc_scans (
            world text NOT NULL,
            xuid text NOT NULL,
            gamertag text NOT NULL,
            mc_day integer NOT NULL CHECK (mc_day >= -1),
            used integer NOT NULL DEFAULT 0 CHECK (used >= 0),
            results jsonb NOT NULL DEFAULT '[]'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (world, xuid, mc_day)
        )
        """
    )
    op.execute("ALTER TABLE app.mc_scans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.mc_scans FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY mc_scans_owner ON app.mc_scans"
        " USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.mc_scans TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.mc_scans")
