"""Admit the `research_reports` / `review_reports` spawn-only personas into the agent CHECKs.

`deep_research`'s `sources=reports` mode (the compare-from-library family,
docs/plans/REPORT_PRESET_PLAN.md P3) spawns two new children: `research_reports` (reads the
owner's stored deep-research reports and compares a dimension across them) and
`review_reports` (the grounding reviewer that checks a comparison draft is faithful to those
reports). Like every spawned child each mints an `app.agent_sessions` row whose `agent` is the
persona, so the `agent IN (...)` CHECK must admit the new names or a reports-mode child INSERT
would fail outright (exactly the widening 0144/0146 did for the library and deep personas). The
`app.tasks` CHECK is kept in lockstep (the two constraints have moved together since 0095),
though both new personas are spawn-only and never owner-selected as a Task.
"""

from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep', 'research_reports',"
    " 'review_reports')"
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
