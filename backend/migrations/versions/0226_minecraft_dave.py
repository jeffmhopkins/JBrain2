"""Minecraft_Dave: the persona, and each player's goals, progress log and memory.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §P1. The owner's Minecraft agent is a persona like
any other, so its sessions need `minecraft_dave` admitted by the two agent CHECKs (the
widening 0192 did for note_ingest). Its data is per **Minecraft player**, keyed like
play history (0224) by xuid — or `name:<gamertag>` for a player the server hasn't seen
yet, re-keyed to the xuid the first time one is known:

- **`mc_goals`**: a player's goals ("Beacon at the base"), open / done / abandoned.
- **`mc_goal_log`**: the progress journal toward them. `source` says who wrote each
  entry: `owner` (the PWA agent), `player` (typed in game, P2), `dave` (Dave's own
  summary, when asked), `auto` (add-on events, M5). Player-typed text is untrusted.
- **`mc_player_memory`**: short durable facts about one player, kept by the agent
  itself — but only through line-level verbs. A replaced or removed line is never
  deleted: it keeps its text with `superseded_at`, and its replacement keeps its
  position (`seq`), so the owner can see and restore a line's history.
- **`mc_chat_player`**: which player a Minecraft_Dave chat is about. The tools read and
  write only that player's data; switching players is an explicit tool call.

All four are **owner-only** (`app.is_owner()`), with isolation tests (CLAUDE.md #3):
players are never database principals, and the in-game door (P2) writes through the
api's owner context on behalf of the xuid the server-side script vouched for.
"""

from alembic import op

revision = "0226"
down_revision = "0225"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt', 'jmolt_observer',"
    " 'note_ingest')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt', 'jmolt_observer',"
    " 'note_ingest', 'minecraft_dave')"
)
_TABLES = ("mc_goals", "mc_goal_log", "mc_player_memory", "mc_chat_player")


def _set_agent_checks(agents: str) -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_agent_check")
    op.execute(
        f"ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_agent_check "
        f"CHECK (agent IN {agents})"
    )
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_agent_check")
    op.execute(f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_agent_check CHECK (agent IN {agents})")


def upgrade() -> None:
    _set_agent_checks(_AGENT_NEW)
    op.execute(
        """
        CREATE TABLE app.mc_goals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            xuid text NOT NULL,
            gamertag text NOT NULL,
            world text,
            title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
            notes text NOT NULL DEFAULT '' CHECK (char_length(notes) <= 500),
            status text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'done', 'abandoned')),
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX mc_goals_xuid_idx ON app.mc_goals (xuid, status)")
    op.execute(
        """
        CREATE TABLE app.mc_goal_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            xuid text NOT NULL,
            gamertag text NOT NULL,
            goal_id uuid REFERENCES app.mc_goals (id) ON DELETE SET NULL,
            at timestamptz NOT NULL DEFAULT now(),
            text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 500),
            source text NOT NULL CHECK (source IN ('owner', 'player', 'dave', 'auto'))
        )
        """
    )
    op.execute("CREATE INDEX mc_goal_log_xuid_at_idx ON app.mc_goal_log (xuid, at)")
    op.execute(
        """
        CREATE TABLE app.mc_player_memory (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            xuid text NOT NULL,
            seq integer NOT NULL,
            text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 300),
            source text NOT NULL CHECK (source IN ('owner', 'player', 'dave')),
            created_at timestamptz NOT NULL DEFAULT now(),
            superseded_at timestamptz
        )
        """
    )
    # One current line per position; history rows (superseded) may share a position.
    op.execute(
        "CREATE UNIQUE INDEX mc_player_memory_current_idx ON app.mc_player_memory (xuid, seq)"
        " WHERE superseded_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE app.mc_chat_player (
            agent_session_id uuid PRIMARY KEY
                REFERENCES app.agent_sessions (id) ON DELETE CASCADE,
            xuid text NOT NULL,
            gamertag text NOT NULL,
            set_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table}"
            " USING (app.is_owner()) WITH CHECK (app.is_owner())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE app.{table}")
    _set_agent_checks(_AGENT_OLD)
