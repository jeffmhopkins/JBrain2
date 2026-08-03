"""jlaunch run index: the owner-only durable mirror of the job launcher.

One row per launched computation (mirrors `jcode_sessions`, 0098): the state/phase the
launcher lists, the finished-run snapshot the results page reads (headline block + machine
specs), and the collected-artifact pointer (content-addressed sha + size). The live PTY and
the checkout live in the jlaunch control server; this table is the durable metadata the PWA
(and, via 0153's share links, a public results page) read. Owner-only RLS; no domain, not
knowledge-base data.

Revision ID: 0152
Revises: 0151
Create Date: 2026-08-03
"""

from alembic import op

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jlaunch_runs (
            -- The control server's job id (hex token), not a uuid.
            id text PRIMARY KEY,
            spec_name text NOT NULL DEFAULT '',
            title text NOT NULL DEFAULT '',
            repo text NOT NULL DEFAULT '',
            state text NOT NULL DEFAULT 'running',
            phase text NOT NULL DEFAULT '',
            verify_ok boolean,
            -- Public-results snapshots; JSONB so the shape can evolve without a migration.
            headline jsonb NOT NULL DEFAULT '[]'::jsonb,
            machine jsonb NOT NULL DEFAULT '{}'::jsonb,
            phase_times jsonb NOT NULL DEFAULT '[]'::jsonb,
            error text NOT NULL DEFAULT '',
            -- The collected artifact: filename, content-addressed sha (into the blob store),
            -- and size. Set at mint time when the api streams the tarball out of the control
            -- server; empty until then.
            artifact_name text NOT NULL DEFAULT '',
            artifact_sha256 text NOT NULL DEFAULT '',
            artifact_bytes bigint,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_active_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.jlaunch_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jlaunch_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY jlaunch_runs_owner ON app.jlaunch_runs
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jlaunch_runs TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.jlaunch_runs")
