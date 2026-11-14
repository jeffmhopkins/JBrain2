"""GrowBot's policy library: walks trained by the Jobs launcher, kept for the Body tab's remote.

The owner trains walking policies on the box (the Jobs launcher's GrowBot jobs, which clone
GrowBot_Training) and asked (2026-11-14) to drive them from the Body tab, pulled straight from
the finished jobs. A job's workspace goes when the job is deleted, so the policy is copied here:

- `policy` — the exported policy JSON as the creature's runner loads it (`obs_spec` carries the
  contract: its channels, `control_hz`, which end it faces and its commands).
- `source_job` / `source_spec` — the Jobs run and spec it came from, for the owner's reference.

Owner-only RLS like 0257's: the sidecar reads a policy for the creature page under the system
owner context after `growbot.token` is checked, and no share, member, device or capability
session reads it.

Revision ID: 0268
Revises: 0267
Create Date: 2026-11-14
"""

from alembic import op

revision = "0268"
down_revision = "0267"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_policies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
            source_job text NOT NULL DEFAULT '',
            source_spec text NOT NULL DEFAULT '',
            policy jsonb NOT NULL CHECK (jsonb_typeof(policy) = 'object'),
            bytes integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.growbot_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.growbot_policies FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY growbot_policies_owner ON app.growbot_policies
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.growbot_policies TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.growbot_policies")
