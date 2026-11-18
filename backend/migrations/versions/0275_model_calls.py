"""Every model call the box makes, so the vitals page can name what is keeping the GPU busy.

The page read "No agent turns" under a GPU pinned at 98% while two GrowBot creatures were
thinking, because creature thinks, code mode and the worker's jobs never appear in the agent
roster. `app.model_calls` is one row per call from every caller (the router and the three
proxies that forward straight to the gateway), opened when the call starts and settled when it
ends (jbrain.model_calls).

Its own table, not `app.box_events`: a thinking creature alone makes thousands of rows a day,
which that table's whole-window read was never sized for. Pruned at a day by the worker.

Owner-only RLS (host telemetry is not domain data), matching app.box_events.

Revision ID: 0275
Revises: 0274
Create Date: 2026-11-18
"""

from alembic import op

revision = "0275"
down_revision = "0274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.model_calls (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            caller text NOT NULL,
            label text NOT NULL DEFAULT '',
            task text NOT NULL DEFAULT '',
            model text NOT NULL DEFAULT '',
            local boolean NOT NULL DEFAULT true,
            persona_id uuid,
            robot text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'running',
            error text NOT NULL DEFAULT '',
            source text NOT NULL DEFAULT ''
        )
        """
    )
    # The window read is "ended inside the window, or still open" (jbrain.model_calls): one
    # index per arm, so a 1 Hz poll never walks the whole retention. The start-time one serves
    # the prune.
    op.execute("CREATE INDEX model_calls_started_idx ON app.model_calls (started_at DESC)")
    op.execute("CREATE INDEX model_calls_ended_idx ON app.model_calls (ended_at DESC)")
    op.execute(
        "CREATE INDEX model_calls_open_idx ON app.model_calls (started_at) WHERE ended_at IS NULL"
    )
    op.execute("ALTER TABLE app.model_calls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.model_calls FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY model_calls_owner ON app.model_calls
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.model_calls TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.model_calls")
