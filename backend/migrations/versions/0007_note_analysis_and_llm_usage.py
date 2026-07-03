"""Phase 3 extraction outputs: per-note analysis header and LLM usage telemetry.

app.note_analysis is the one-row-per-note product of the note.extract call
(title, tags, watermark); facts/entities/tokens from the same call live in the
0006 tables. It carries the note's domain and the standard has_domain_scope
policy so a sensitive auto-title can never leak through a scoped session.

app.llm_usage is telemetry, not domain data (docs/reference/ANALYSIS.md "Token
accounting"): rows describe adapter calls, never note content, so the policy
is owner-only rather than domain-scoped. Insert-only; aggregation happens at
query time over created_at.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-10
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.note_analysis (
            note_id uuid PRIMARY KEY REFERENCES app.notes(id) ON DELETE CASCADE,
            title text,
            tags text[] NOT NULL DEFAULT '{}',
            extractor text,
            prompt_version text,
            analyzed_at timestamptz,
            domain_code text NOT NULL REFERENCES app.domains(code)
        )
        """
    )
    op.execute("ALTER TABLE app.note_analysis ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.note_analysis FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY note_analysis_domain ON app.note_analysis
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    # UPDATE because re-analysis upserts on note_id; never DELETE (the cascade
    # from notes is the only removal path).
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.note_analysis TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.llm_usage (
            id uuid PRIMARY KEY,
            task text NOT NULL,
            provider text NOT NULL,
            model text NOT NULL,
            input_tokens int NOT NULL,
            output_tokens int NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX llm_usage_created_idx ON app.llm_usage (created_at)")
    op.execute("ALTER TABLE app.llm_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.llm_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY llm_usage_owner ON app.llm_usage
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT ON app.llm_usage TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.llm_usage")
    op.execute("DROP TABLE app.note_analysis")
