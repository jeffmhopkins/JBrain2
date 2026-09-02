"""Every command attempt heard on the air, accepted or refused.

docs/plans/APRS_CONTROL_PLAN.md P4 — "every attempt is visible" is an exit criterion,
and a push notification does not satisfy it: pushes are ephemeral, arrive only if the
owner registered a device, and are exactly what an attacker would hope goes unread.
The security value is the RECORD — three refusals against `GATE` from an unknown
station last Tuesday is a fact the owner must be able to find later.

**A refusal is the interesting row**, so the table is written on the reject path first
and the accept path second. `reason` is the box's own words, never the packet's.

**Nothing here is a credential.** The offered code is stored because a spent code is
worthless (the counter has moved past it) and because seeing what was tried is how the
owner tells a mis-key from a probe. The key never appears.

`task_id` is nullable and ON DELETE SET NULL: an attempt against a command word that
matched no task is precisely the attempt worth keeping, and deleting the task must not
erase the history of what was tried against it.

Owner-only, like the heard log it comes from (CLAUDE.md #3).
"""

from alembic import op

revision = "0182"
down_revision = "0181"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.command_attempts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            heard_at timestamptz NOT NULL DEFAULT now(),
            task_id uuid REFERENCES app.tasks(id) ON DELETE SET NULL,
            source text NOT NULL,
            word text NOT NULL,
            code text NOT NULL,
            accepted boolean NOT NULL,
            reason text NOT NULL,
            run_id uuid
        )
        """
    )
    op.execute("CREATE INDEX command_attempts_heard_idx ON app.command_attempts (heard_at DESC)")
    op.execute("ALTER TABLE app.command_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.command_attempts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY command_attempts_owner ON app.command_attempts
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # The grant is what gets the app role to the table; the policy above decides what it
    # sees once there. DELETE is granted because retention will need it — RLS keeps a
    # non-owner's delete matching no rows.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.command_attempts TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.command_attempts")
