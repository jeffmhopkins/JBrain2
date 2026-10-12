"""`agent_sessions.origin` — where an owner chat was started: the desk panel, or the PWA.

docs/plans/JPANEL_C6_PLAN.md, owner decision 2026-10-12 (firmware-c6 0.0.15): a long press in
the panel's Jerv or Dave thread lists that agent's recent conversations to switch between —
the ones started at the panel and the ones started in the PWA, each marked by where it began,
and never the in-game Minecraft chats, a scheduled task's run, a sub-agent or a note
conversation. Nothing on the row said which was which: the panel's thread table holds only the
CURRENT thread per agent, and every maker writes the same columns. So the two makers that
count stamp it at creation (`panel_agent.panel_thread` "panel", `POST /api/sessions` "pwa") and
every other maker leaves it NULL, which keeps a new kind of session out of the list until
someone decides it belongs.

A column on an owner-only table, so it is under `agent_sessions_owner` (migration 0015) like
every other column there; `test_agent_session_origin_rls` pins that a panel's own context reads
none of it.

**The backfill** marks what can be told apart from the rows alone, and leaves the rest NULL:
- "panel": a root Jerv or Dave session a panel's thread points at, or still titled "Desk panel"
  (the title the panel gave every thread it made before this).
- "pwa": any other root Jerv or Dave session that is not the game door's (`mc_chat_player`), a
  task's run (`task_runs`) or a note conversation (`note_conversations`).

Revision ID: 0238
Revises: 0237
Create Date: 2026-10-12
"""

from alembic import op

revision = "0238"
down_revision = "0237"
branch_labels = None
depends_on = None

#: Named so the integration suite runs the same statements against seeded rows.
BACKFILL_PANEL = """
    UPDATE app.agent_sessions s SET origin = 'panel'
    WHERE s.origin IS NULL AND s.parent_session_id IS NULL
      AND s.agent IN ('jerv', 'minecraft_dave')
      AND (s.title = 'Desk panel'
           OR EXISTS (SELECT 1 FROM app.endpoint_panel_thread t WHERE t.agent_session_id = s.id))
"""
BACKFILL_PWA = """
    UPDATE app.agent_sessions s SET origin = 'pwa'
    WHERE s.origin IS NULL AND s.parent_session_id IS NULL
      AND s.agent IN ('jerv', 'minecraft_dave')
      AND NOT EXISTS (SELECT 1 FROM app.mc_chat_player c WHERE c.agent_session_id = s.id)
      AND NOT EXISTS (SELECT 1 FROM app.task_runs r WHERE r.session_id = s.id)
      AND NOT EXISTS (SELECT 1 FROM app.note_conversations n WHERE n.session_id = s.id)
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.agent_sessions ADD COLUMN origin text CHECK (origin IN ('panel', 'pwa'))"
    )
    op.execute(BACKFILL_PANEL)
    op.execute(BACKFILL_PWA)
    # The Recent list: one agent's root chats from these two makers, newest first.
    op.execute(
        "CREATE INDEX agent_sessions_origin_recent_idx ON app.agent_sessions"
        " (principal_id, agent, last_active_at DESC) WHERE origin IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.agent_sessions_origin_recent_idx")
    op.execute("ALTER TABLE app.agent_sessions DROP COLUMN origin")
