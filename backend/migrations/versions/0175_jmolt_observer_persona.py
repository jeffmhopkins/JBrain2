"""Admit the `jmolt_observer` persona into the agent CHECKs.

jmolt_observer is a new owner-selectable, read-only analyst persona (docs/plans/JMOLT_PLAN.md,
W4, M16): a KB-less, egress-toolless lens on jmolt through the `jmolt_observe` umbrella. Like
every persona it mints an `app.agent_sessions` row whose `agent` is the persona name, so the
`agent IN (...)` CHECK must admit `'jmolt_observer'` or the session INSERT would fail (exactly
the widening 0144/0146/0154/0164/0171 did for the other personas). The two agent CHECKs
(`agent_sessions` + `tasks`) have moved together since 0095 and are kept in lockstep here,
even though this interactive persona never runs from a `tasks` row.
"""

from alembic import op

revision = "0175"
down_revision = "0174"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt', 'jmolt_observer')"
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
