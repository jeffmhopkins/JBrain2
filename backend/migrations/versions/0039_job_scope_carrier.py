"""The E1 scope carrier: a triggering principal + fail-closed domain stamp on a job.

Today every job runs under the all-domains `queue.SYSTEM_CTX` (the worker is the
owner's own cross-domain machinery). Owner/agent-triggered jobs must instead run
under the *narrowed* scope of whatever triggered them — the no-confused-deputy
property (docs/archive/WORKFLOW_ENGINE_PLAN.md §2 E1, ASSISTANT.md I-8). This migration
adds the two nullable columns that carry that scope on the job row:

- `principal_id` — the triggering principal (NULL for a system job);
- `domain_code` — the most-restrictive domain the trigger touched, FK
  `app.domains.code` so a stamp can only name a real domain (NULL for a system
  job).

Both NULL = a system job (every job today), so existing rows and the six shipped
kinds are unchanged. The worker reads the stamp and builds a narrowed
`SessionContext` for a *stamped* job; an unstamped job keeps `SYSTEM_CTX`. RLS on
`app.jobs` is unchanged: it stays owner-only (the table holds row IDs only, never
domain content — the stamp is a scope *to run under*, not domain data to firewall),
so no new policy and no RLS-isolation test for this migration (the jobs policy
already has one in test_queue_pg).

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-15
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.jobs
        ADD COLUMN principal_id uuid,
        ADD COLUMN domain_code text REFERENCES app.domains(code)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.jobs DROP COLUMN principal_id, DROP COLUMN domain_code")
