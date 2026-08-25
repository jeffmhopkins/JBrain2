"""Admit the `jmolt` persona into the agent CHECKs.

jmolt is a new owner-selectable, non-owner-facing persona (docs/plans/JMOLT_PLAN.md):
an autonomous nocturnal agent that lives one hour a night on Moltbook, sandboxed away
from the knowledge base. Like every persona it mints an `app.agent_sessions` row whose
`agent` is the persona name — and, because it runs from a nightly `app.tasks` row, a
`tasks` row too — so both `agent IN (...)` CHECKs must admit `'jmolt'` or the session
and task INSERTs would fail outright (exactly the widening 0144/0146/0154/0164 did for
the library/deep/reports/scout personas). The two constraints have moved together since
0095 and are kept in lockstep here.
"""

from alembic import op

revision = "0171"
down_revision = "0170"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch', 'jmolt')"
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
