"""Research-report display title — a short, LLM-generated heading per report (0140).

A deep-research report is keyed on the owner's raw `question`, which is often a full
paragraph. The Research Library browses these, so a report needs a tight display title,
the way an analysed video carries its channel-given `title`. This adds a nullable
`title` column filled by the `title_research_report` job (external.report_titler), a
follow-up of `persist_report` exactly like the `embed_research_report` embedding fill —
so a title failure never blocks the report the owner already sees, and the listing
falls back to the question until the title lands.

Existing rows are backfilled by enqueuing one title job per titleless report: the job
is idempotent (it re-checks `title IS NULL`), so a re-run is a harmless no-op.

Revision ID: 0141
Revises: 0140
Create Date: 2026-07-20
"""

from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.research_reports ADD COLUMN title text")
    # Backfill: enqueue a title job for every already-stored report. A system job
    # (NULL principal/domain), the same shape persist_report enqueues, picked up by
    # the worker once the title_research_report handler is deployed with it.
    op.execute(
        """
        INSERT INTO app.jobs (id, kind, payload)
        SELECT gen_random_uuid(), 'title_research_report',
               jsonb_build_object('report_id', id::text)
        FROM app.research_reports
        WHERE title IS NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.research_reports DROP COLUMN title")
