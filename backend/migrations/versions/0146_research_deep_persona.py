"""Admit the `research_deep` task-agent persona into the agent CHECKs.

The deepest-research two-tier recursion (docs/plans/DEEPEST_RESEARCH_TOOL_PLAN.md, R2)
adds one spawn-only persona: `research_deep`, the TASK-AGENT tier that may split its
sub-question into a bounded fan of depth-2 sub agents via the one-shot
`decompose_research` tool. Like every spawned child it mints an `app.agent_sessions`
row whose `agent` is the persona, so the `agent IN (...)` CHECK must admit the new name
or a two-tier child INSERT would fail outright (exactly the widening 0144 did for the
library personas). The `app.tasks` CHECK is kept in lockstep (the two constraints have
moved together since 0095), though `research_deep` is spawn-only and never owner-selected
as a Task.
"""

from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None

_AGENT_OLD = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library')"
)
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library', 'research_deep')"
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
