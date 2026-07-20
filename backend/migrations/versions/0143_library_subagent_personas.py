"""Admit the two video-library sub-agent personas into the agent CHECKs.

`deep_research`'s `sources=library` / `library_first` modes spawn `research_library`
and `review_library` children (the corpus twins of `research`/`review`, searching the
external-video library instead of the web — DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md). Like
every spawned child, each mints an `app.agent_sessions` row whose `agent` is the
persona, so the `agent IN (...)` CHECK must admit the two new names or a library-mode
child INSERT would fail outright (exactly the widening 0105 did for the web personas).
The `app.tasks` CHECK is kept in lockstep (the two constraints have moved together
since 0095), though these personas are spawn-only and never owner-selected as a Task.

Numbered 0143 (originally 0141): PR #910 landed its own 0141 (`research_report_title`)
in the same merge window as this feature's original 0141/0142, so this migration is
re-sequenced onto the tail of the chain — 0140 → 0141 (title) → 0142 (source_mode) →
0143 (this) — to resolve the duplicate-revision collision on main.
"""

from alembic import op

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None

_AGENT_OLD = "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize')"
_AGENT_NEW = (
    "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize',"
    " 'research_library', 'review_library')"
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
