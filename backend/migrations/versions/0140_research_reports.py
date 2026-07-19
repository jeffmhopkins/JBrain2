"""Research-report library — one row per completed deep-research run (DEEP_RESEARCH_TOOL_PLAN.md).

A durable home for the `deep_research` tool's synthesized reports, mirroring the external-video
corpus (`external_sources`, 0133): third-party-sourced content the assistant can search and re-open
but never treats as owner knowledge (#7). A report has no timeline, so — unlike the video corpus —
there is no sibling chunks table: the keyword leg is a generated `tsv` over the question + report,
the dense leg a source-level `summary_embedding` (bge-small-en-v1.5, 384 dims, HNSW cosine), both
filled by the `embed_research_report` job. Reports are few and short next to a video's fused
timeline, so a single row per report is the right granularity.

Carries the corpus `external` domain (0136) + the standard `app.has_domain_scope(domain_code)`
firewall (0002), so jerv's `external`-scoped corpus context reaches reports and nothing
owner-authored. `question_hash` (sha256 of the normalized question) is UNIQUE: a re-run of the same
question upserts, so the newest report wins and the row doubles as a dedup ledger.

Revision ID: 0140
Revises: 0139
Create Date: 2026-07-19
"""

from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.research_reports (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- the jerv session that produced the report (provenance; a run always has one).
            session_id uuid,
            question text NOT NULL,
            -- sha256 of the normalized question — the UNIQUE dedup key (a re-run upserts).
            question_hash text NOT NULL,
            report_md text NOT NULL,
            -- a short display/search excerpt (the report's opening); the source-level
            -- "which report" vector is built from it by the embed_research_report job.
            summary text,
            summary_embedding vector(384),
            embedding_model text,
            -- the keyword leg: question + report body, markers-free (Markdown is prose enough).
            tsv tsvector GENERATED ALWAYS AS (
                to_tsvector('english', coalesce(question, '') || ' ' || coalesce(report_md, ''))
            ) STORED,
            -- provenance / view-rebuild slots (the deep_research_report view's data shape).
            complexity text,
            rounds integer NOT NULL DEFAULT 1,
            sub_agents integer NOT NULL DEFAULT 0,
            analyzed boolean NOT NULL DEFAULT false,
            revised boolean NOT NULL DEFAULT false,
            coverage_limited boolean NOT NULL DEFAULT false,
            truncated boolean NOT NULL DEFAULT false,
            -- the global citation registry [{url, title}] so [^n] markers re-render on show.
            sources jsonb NOT NULL DEFAULT '[]'::jsonb,
            -- pipeline provenance (the router spec string that produced the synthesis).
            tool text,
            status text NOT NULL DEFAULT 'done'
                CHECK (status IN ('done', 'unavailable')),
            created_at timestamptz NOT NULL DEFAULT now(),
            domain_code text NOT NULL DEFAULT 'external' REFERENCES app.domains(code),
            UNIQUE (question_hash)
        )
        """
    )
    op.execute(
        "CREATE INDEX research_reports_status_idx ON app.research_reports (status, created_at)"
    )
    op.execute("CREATE INDEX research_reports_tsv_idx ON app.research_reports USING GIN (tsv)")
    op.execute(
        "CREATE INDEX research_reports_summary_embedding_idx"
        " ON app.research_reports USING hnsw (summary_embedding vector_cosine_ops)"
    )

    op.execute("ALTER TABLE app.research_reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.research_reports FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY research_reports_domain ON app.research_reports
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    # DELETE because a re-run replaces a question's report wholesale, and the owner may prune.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.research_reports TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.research_reports")
