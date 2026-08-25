"""jmolt's write outbox + action ledger (docs/plans/JMOLT_PLAN.md, W3).

`app.jmolt_outbox` stages every Moltbook WRITE (post/comment/vote/follow/subscribe/
profile). A staged row is `queued` until released — by the owner in the PWA when the
autonomy switch is off, or automatically when it is on — then the drip sweep publishes it
and marks it `published`/`failed`. `app.jmolt_action_ledger` is the append-only complete
record of everything jmolt DID — every write AND every web egress — with the fenced
content it was reacting to (M14), for jerv's W4 observation.

The authority split is the load-bearing part (M7):
- **Outbox INSERT** (stage) is jmolt-only (`auth_ctx='jmolt'`, principal-pinned) — jmolt's
  tools stage rows.
- **Outbox UPDATE** (release / discard / publish / fail) requires an owner who is NOT
  jmolt (`is_owner() AND auth_ctx() <> 'jmolt'`): the PWA owner and the system sweep can
  advance a row, but jmolt's own session can NEVER move a row to `released` — so it cannot
  self-publish past the review queue even if it somehow gained an update path.
- **SELECT** on both is the jmolt domain scope (jmolt, the owner/PWA, jerv's observation).
- The **ledger** is append-only: jmolt and the system may INSERT (principal-pinned); only a
  non-jmolt owner (the system retention prune) may DELETE; no one may UPDATE.
"""

from alembic import op

revision = "0174"
down_revision = "0173"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jmolt_outbox (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            kind text NOT NULL CHECK (
                kind IN ('post', 'comment', 'vote', 'follow', 'subscribe', 'profile')
            ),
            payload jsonb NOT NULL,
            dedup_key text,
            status text NOT NULL DEFAULT 'queued' CHECK (
                status IN ('queued', 'released', 'published', 'failed', 'discarded')
            ),
            publish_at timestamptz,
            moltbook_id text,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            published_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX jmolt_outbox_due_idx ON app.jmolt_outbox (status, publish_at)"
        " WHERE status IN ('queued', 'released')"
    )
    op.execute(
        """
        CREATE TABLE app.jmolt_action_ledger (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            action text NOT NULL,
            target text,
            reacted_to text,
            detail jsonb,
            at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX jmolt_action_ledger_idx ON app.jmolt_action_ledger (principal_id, seq DESC)"
    )

    for table in ("jmolt_outbox", "jmolt_action_ledger"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_read ON app.{table}
            FOR SELECT USING (app.has_domain_scope('jmolt'))
            """
        )

    # Outbox: jmolt stages (INSERT), a non-jmolt owner advances (UPDATE).
    op.execute(
        """
        CREATE POLICY jmolt_outbox_stage ON app.jmolt_outbox
        FOR INSERT WITH CHECK (
            app.auth_ctx() = 'jmolt'
            AND principal_id = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute(
        """
        CREATE POLICY jmolt_outbox_advance ON app.jmolt_outbox
        FOR UPDATE USING (app.is_owner() AND app.auth_ctx() <> 'jmolt')
        WITH CHECK (app.is_owner() AND app.auth_ctx() <> 'jmolt')
        """
    )
    # A non-jmolt owner (the system) may purge terminal rows — jmolt never can.
    op.execute(
        """
        CREATE POLICY jmolt_outbox_purge ON app.jmolt_outbox
        FOR DELETE USING (app.is_owner() AND app.auth_ctx() <> 'jmolt')
        """
    )

    # Ledger: jmolt and the system append (principal-pinned); the system prunes (DELETE).
    op.execute(
        """
        CREATE POLICY jmolt_action_ledger_append ON app.jmolt_action_ledger
        FOR INSERT WITH CHECK (
            (app.auth_ctx() = 'jmolt' OR (app.is_owner() AND app.auth_ctx() <> 'jmolt'))
            AND principal_id = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute(
        """
        CREATE POLICY jmolt_action_ledger_prune ON app.jmolt_action_ledger
        FOR DELETE USING (app.is_owner() AND app.auth_ctx() <> 'jmolt')
        """
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jmolt_outbox TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.jmolt_outbox_seq_seq TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON app.jmolt_action_ledger TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.jmolt_action_ledger_seq_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jmolt_action_ledger")
    op.execute("DROP TABLE IF EXISTS app.jmolt_outbox")
