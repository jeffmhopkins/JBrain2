"""Image-gen (Wave G1): the `generated_images` chat-artifact table.

Generated images are chat artifacts, not domain facts, so the table is owner-only — it
mirrors `wiki_articles`/`wiki_talk_*` (`app.is_owner()` USING+CHECK, FORCE RLS), NOT
domain-scoped: jerv runs `reads_knowledge_base=False` with empty read scopes, so there is no
domain to attribute. Rows are immutable provenance of a generation (no UPDATE grant); the
result PNG lives in the blob store, this row is the by-id metadata + seed for repeatability.

Revision ID: 0077
Revises: 0076
Create Date: 2026-06-20
"""

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.generated_images (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            blob_sha256 text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('generate', 'edit')),
            model text NOT NULL,
            prompt text NOT NULL,
            source_sha256 text,
            width int NOT NULL,
            height int NOT NULL,
            steps int NOT NULL,
            seed bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX generated_images_created_idx ON app.generated_images (created_at DESC)"
    )
    op.execute("ALTER TABLE app.generated_images ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.generated_images FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY generated_images_owner ON app.generated_images
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # No UPDATE — rows are immutable generation provenance.
    op.execute("GRANT SELECT, INSERT, DELETE ON app.generated_images TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.generated_images")
