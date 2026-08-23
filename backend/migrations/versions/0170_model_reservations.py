"""The local-model reservation ledger: one row per model INSTANCE, two size columns.

WHY A TABLE AND NOT A CALCULATION. This box hard-locked twice on model loads that never
passed a memory gate, and each fix failed the same way: a gate READ MEMORY and compared it
against a prediction. Measurements cannot see a transition. A model that has been asked to
stop still holds every byte; a model three seconds into a 200 s load has committed almost
none of what it will hold. Two processes (the api and the worker) each read those instants
independently, at different moments, against different floors — and any correction applied to
one layer was absent from the other. See docs/plans/LOCAL_MODEL_LEDGER_PLAN.md.

Admission therefore moves to DECLARATIONS. A row is charged before anything is spawned and
discharged only when the process is confirmed dead, so the window a measurement cannot see is
exactly the window this table covers. Prior art is unanimous: Kubernetes admits against
requests, Linux's CommitLimit refuses against promises even with RAM free, and Borg dares use
measured usage only for a tier it is willing to kill — which this box does not have, because
every model here is prod and the failure mode is a host lock-up, not a rescheduled pod.

WHY IT IS A ROW PER INSTANCE AND NOT PER MODEL. A restart with a changed config is two
instances: the old one draining at its old declared size (we no longer have the config that
produced it) and the new one planned at its new size. Two rows sum to the right answer whether
the teardown finishes first or the two overlap. A row keyed by model id would have to be
mutated in place, which is the same lie in table form.

WHY TWO SIZE COLUMNS AND NOT TWO TABLES. On Strix Halo the iGPU draws GTT from system RAM, so
device memory is a SUBSET of host memory, not a second pool. Keeping both figures on one row
makes double-counting across the two admission layers UNREPRESENTABLE rather than something
each layer has to remember to correct — which is the specific defect that reopened three
times. (It also means a future vLLM-style sleep, which moves weights off the device while the
server lives, is bytes moving between two columns of one row.)

Owner-only RLS with no domain predicate, like the rest of the operational tables (box_events,
deploy_history, host_metrics): this is the box's own bookkeeping about its own hardware, and
both writers are the owner's machinery. DELETE is granted because discharge is a delete — the
absence of a row is what "this instance is dead" means here, and a tombstone phase would be
one more state to get wrong.

Revision ID: 0170
Revises: 0169
Create Date: 2026-08-23
"""

from alembic import op

revision = "0170"
down_revision = "0169"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.model_reservations (
            instance_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            served_model text NOT NULL,
            phase text NOT NULL DEFAULT 'planned',
            host_gb double precision NOT NULL,
            device_gb double precision NOT NULL,
            declared_at timestamptz NOT NULL DEFAULT now(),
            phase_at timestamptz NOT NULL DEFAULT now(),
            source text NOT NULL DEFAULT ''
        )
        """
    )
    # The only read is "the whole ledger" — admission sums every live row, and there are at
    # most a handful — so no index is warranted beyond the primary key. The one query that is
    # not a full scan is the TTL sweep, which orders by `phase_at`.
    op.execute("CREATE INDEX model_reservations_phase_at_idx ON app.model_reservations (phase_at)")
    op.execute("ALTER TABLE app.model_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.model_reservations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY model_reservations_owner ON app.model_reservations
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # Charge (INSERT), advance a phase (UPDATE), discharge and sweep (DELETE), admit (SELECT)
    # — from both the api and the worker.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.model_reservations TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.model_reservations")
