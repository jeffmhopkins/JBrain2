"""DELETE grant on app.runs for reaping idle housekeeping-sweep runs.

The reconcile/geofence sweeps fire every few minutes; a fire that reconciles
nothing leaves a 0-work run behind, flooding the Ops "Runs" log. The worker now
reaps such a run right after the (idle) sweep job completes
(jbrain.workflow.runlog.reap_idle_run). That is the run log's second sanctioned
DELETE path (after 0037's cascade from a deleted agent session): the run reader
otherwise only SELECTs, and supersede only UPDATEs, so app.runs was granted
SELECT/INSERT/UPDATE but never DELETE (0016). Grant it here.

The `runs_owner` RLS policy is `USING (app.is_owner())` with no FOR clause (so it
already covers DELETE for the owner's own rows), and the run_steps cascade runs as
the table owner (RLS/privilege bypassed), so only this table-level grant is needed.

Revision ID: 0129
Revises: 0128
Create Date: 2026-07-09
"""

from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON app.runs TO jbrain_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON app.runs FROM jbrain_app")
