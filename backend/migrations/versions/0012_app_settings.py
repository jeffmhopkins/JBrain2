"""Server-side user settings: the first server-synced preference store.

app.settings is a one-row-per-key jsonb store for owner preferences that must
follow the account across devices — `image_analysis_mode` ("full" | "ocr",
default full) is the first; theme and text size deliberately stay local to
the device. Owner-only RLS (app.is_owner() — settings are owner data, the
llm_usage pattern). An absent row means "use the default": readers fall back
in code, so the table never needs seeding, and no DELETE grant — resetting a
setting is upserting its default.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-11
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.settings (
            key text PRIMARY KEY,
            value jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.settings FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY settings_owner ON app.settings
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.settings TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.settings")
