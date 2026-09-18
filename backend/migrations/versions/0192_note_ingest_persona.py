"""Admit the `note_ingest` persona into the agent CHECKs.

note_ingest is the note conversation (docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, D1/D16):
an owner persona the engine opens with a captured note as turn 0, running under its own
closed, empty tool allowlist rather than curator's wildcard. Like every persona it mints an
`app.agent_sessions` row whose `agent` is the persona name, so the `agent IN (...)` CHECK
must admit `'note_ingest'` or the session INSERT would fail (the widening 0144/0146/0154/
0164/0171/0175 each did for their persona). The two agent CHECKs (`agent_sessions` +
`tasks`) have moved together since 0095 and are kept in lockstep here, even though a note
conversation is opened by the ingest path and never from a `tasks` row.
"""

from alembic import op

revision = "0192"
down_revision = "0191"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt', 'jmolt_observer')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt', 'jmolt_observer',"
    " 'note_ingest')"
)


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


def downgrade() -> None:
    _set_agent_checks(_AGENT_OLD)
