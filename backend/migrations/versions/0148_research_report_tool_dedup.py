"""Tool-aware dedup for research reports (docs/plans/DEEPEST_RESEARCH_TOOL_PLAN.md, R7).

`deepest_research` lands its report in the same `app.research_reports` library as
`deep_research`, but the dedup key was `UNIQUE (question_hash)` alone — so a deep and a
deepest report on the SAME question would clobber each other (newest-wins). Widen the key
to `(question_hash, tool)` so the two coexist: a bounded deep_research report and the
no-holds deepest report on one question are distinct rows the owner can compare.

`tool` becomes NOT NULL DEFAULT 'deep_research' (a unique key over a nullable column treats
NULLs as distinct, which would silently defeat dedup); existing rows — all written by
`deep_research` with a NULL tool — backfill to 'deep_research'.
"""

from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill the existing (deep_research-only) rows, then make the column dedup-safe.
    op.execute("UPDATE app.research_reports SET tool = 'deep_research' WHERE tool IS NULL")
    op.execute("ALTER TABLE app.research_reports ALTER COLUMN tool SET DEFAULT 'deep_research'")
    op.execute("ALTER TABLE app.research_reports ALTER COLUMN tool SET NOT NULL")
    # Swap the single-column unique for the tool-aware one.
    op.execute(
        "ALTER TABLE app.research_reports DROP CONSTRAINT research_reports_question_hash_key"
    )
    op.execute(
        "ALTER TABLE app.research_reports"
        " ADD CONSTRAINT research_reports_question_tool_key UNIQUE (question_hash, tool)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.research_reports DROP CONSTRAINT research_reports_question_tool_key"
    )
    op.execute(
        "ALTER TABLE app.research_reports"
        " ADD CONSTRAINT research_reports_question_hash_key UNIQUE (question_hash)"
    )
    op.execute("ALTER TABLE app.research_reports ALTER COLUMN tool DROP NOT NULL")
    op.execute("ALTER TABLE app.research_reports ALTER COLUMN tool DROP DEFAULT")
