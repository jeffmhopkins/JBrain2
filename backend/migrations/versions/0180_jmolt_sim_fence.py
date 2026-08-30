"""jmolt simulator fence: a `sim` flag on the outbox.

The simulator (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S1) runs a real jmolt night against a
recorded corpus so a design change can be measured in seconds instead of a night and one
sample. It deliberately reuses the PRODUCTION write path — the outbox is the chokepoint every
guard hangs off, and a simulator that bypasses it measures a different system.

That makes one thing load-bearing. A sim night runs under its own principal, and every other
outbox and action-ledger query is principal-scoped, so that alone keeps sim rows out of the
caps, the near-duplicate check, the digest and the observer. `OutboxRepo.due` is the one
exception: the drip sweep runs box-wide, so it selects released rows with no principal filter
and would publish a simulated write to the real Moltbook. This column is what makes that
impossible rather than merely unlikely.

NOT NULL DEFAULT false, so every existing row is real and every caller that does not know
about the simulator keeps staging real rows. The action ledger gets no such column: nothing
reads it box-wide, and a column with no reader is a claim we would stop maintaining.
"""

from alembic import op

revision = "0180"
down_revision = "0179"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.jmolt_outbox ADD COLUMN sim boolean NOT NULL DEFAULT false")
    # The drip sweep's hot query is `status = 'released' AND NOT sim` — keep it on an index.
    op.execute(
        "CREATE INDEX jmolt_outbox_publishable"
        " ON app.jmolt_outbox (status, publish_at)"
        " WHERE NOT sim"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.jmolt_outbox_publishable")
    op.execute("ALTER TABLE app.jmolt_outbox DROP COLUMN sim")
