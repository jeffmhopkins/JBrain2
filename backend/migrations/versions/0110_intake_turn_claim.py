"""DB-atomic turn claim + atomic capture for the intake chat (W3 hardening).

The first cut held the per-session turn/cost caps and the concurrency cap in an
in-process set, which a multi-worker deploy would silently defeat (red-team H2/L1).
This moves both into Postgres, the same atomic-claim pattern as redeem/capture:

  * `in_flight` — a per-session turn lock. A turn is claimed by one conditional
    UPDATE (`NOT in_flight AND turns_used < cap …`), so concurrency=1 and the
    cumulative caps hold across workers. The lock is self-healing: a claim reclaims
    a lock whose turn started longer ago than the stale window (a crashed turn never
    locks the session forever).

  * the `intake_submissions` INSERT policy gains the `bootstrap` auth-context, so
    capture can burn the run, flip the session, and write the submission in ONE
    transaction (serialized by the session-status flip) — two concurrent confirms
    can no longer both burn a run / double-submit.
"""

from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.intake_sessions ADD COLUMN in_flight boolean NOT NULL DEFAULT false"
    )
    op.execute("DROP POLICY intake_submissions_insert ON app.intake_submissions")
    op.execute(
        """
        CREATE POLICY intake_submissions_insert ON app.intake_submissions FOR INSERT
        WITH CHECK (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() = 'bootstrap'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY intake_submissions_insert ON app.intake_submissions")
    op.execute(
        """
        CREATE POLICY intake_submissions_insert ON app.intake_submissions FOR INSERT
        WITH CHECK (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute("ALTER TABLE app.intake_sessions DROP COLUMN in_flight")
