"""Admit the `research_scout` / `research_fetch` spawn-only personas into the agent CHECKs.

`deep_research`'s two-phase (scout → read) gather (docs/plans/REPORT_PRESET_PLAN.md) spawns two
new children: `research_scout` (SEARCH-only — surfaces candidate URLs for an angle) and
`research_fetch` (FETCH-only — opens those URLs and reports from the page text). Like every
spawned child each mints an `app.agent_sessions` row whose `agent` is the persona, so the
`agent IN (...)` CHECK must admit the new names or a scout/read child INSERT would fail outright
(exactly the widening 0144/0146/0154 did for the library, deep, and reports personas). The
`app.tasks` CHECK is kept in lockstep (the two constraints have moved together since 0095),
though both new personas are spawn-only and never owner-selected as a Task.
"""

from alembic import op

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports', 'research_scout', 'research_fetch')"
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
