"""`deploy_history` — the version the RUNNING server was built from, one row per
change (docs/runbooks/DEBUG_ACCESS.md).

Operational telemetry, owner-only (a build revision is not domain data): `app.is_owner()`
with FORCE RLS, exactly like `host_metrics` (0082). On boot the app appends a row ONLY
when its baked `git_sha` differs from the newest existing row, so the table is a deduped
timeline of what shipped and from when — enough to answer, for any timestamped record
(a research run, an ingest), which build produced it.

Revision ID: 0160
Revises: 0159
Create Date: 2026-08-09
"""

from alembic import op

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.deploy_history (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- The build provenance baked into the running image (config.Settings.git_*).
            git_sha text NOT NULL,
            git_describe text NOT NULL DEFAULT 'unknown',
            build_time text NOT NULL DEFAULT 'unknown',
            -- When this version was FIRST seen running — the boot that recorded it, which
            -- is the moment the deploy actually took effect (the container recreated onto
            -- the new image), not merely when an update was attempted.
            deployed_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The two reads this table serves — "newest first" history and the dedupe check on
    # boot — both want the latest rows, so index deployed_at descending.
    op.execute(
        "CREATE INDEX deploy_history_deployed_at_idx ON app.deploy_history (deployed_at DESC)"
    )
    op.execute("ALTER TABLE app.deploy_history ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.deploy_history FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY deploy_history_owner ON app.deploy_history
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.deploy_history TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.deploy_history")
