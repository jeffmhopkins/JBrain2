"""The archivist's Gmail metadata index (docs/EMAIL_ARCHIVIST_PLAN.md).

Gmail has no server-side group-by, so exact, full-history sender analytics need a local
index: one row per message holding just `From`/`Date`/`labels` (never the body), built
once by a resumable backfill and kept current incrementally. `gmail_message_meta` is the
index; `gmail_index_state` is the single per-principal control/progress row the backfill
checkpoints against and the Settings panel polls for live progress.

Both are owner-only metadata (RLS `is_owner()`, like `archivist_memory`/`tasks`,
migration 0015) — the archivist's own derived data, not the owner's knowledge base, so
the read-nothing sandbox is intact. Each needs the mandatory RLS isolation test.

Revision ID: 0096
Revises: 0095
Create Date: 2026-06-25
"""

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.gmail_message_meta (
            principal_id text NOT NULL,
            gmail_id text NOT NULL,
            thread_id text NOT NULL DEFAULT '',
            state text NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'done', 'error')),
            sender_email text NOT NULL DEFAULT '',
            sender_domain text NOT NULL DEFAULT '',
            subject text NOT NULL DEFAULT '',
            sent_at timestamptz,
            label_ids text[] NOT NULL DEFAULT '{}',
            error text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (principal_id, gmail_id)
        )
        """
    )
    # The backfill claims this principal's not-yet-fetched rows — a partial index so the
    # "find pending work" scan stays cheap as the done set grows into the hundreds of
    # thousands.
    op.execute(
        "CREATE INDEX gmail_message_meta_pending_idx ON app.gmail_message_meta (principal_id)"
        " WHERE state = 'pending'"
    )
    # The two aggregate access paths the query tools drive: rank by domain, and bucket by
    # day. Scoped by principal (RLS already filters, but the index keys it too).
    op.execute(
        "CREATE INDEX gmail_message_meta_domain_idx"
        " ON app.gmail_message_meta (principal_id, sender_domain) WHERE state = 'done'"
    )
    op.execute(
        "CREATE INDEX gmail_message_meta_sent_idx"
        " ON app.gmail_message_meta (principal_id, sent_at) WHERE state = 'done'"
    )
    op.execute("ALTER TABLE app.gmail_message_meta ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.gmail_message_meta FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY gmail_message_meta_owner ON app.gmail_message_meta
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.gmail_message_meta TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.gmail_index_state (
            principal_id text PRIMARY KEY,
            phase text NOT NULL DEFAULT 'idle'
                CHECK (phase IN ('idle', 'discovering', 'fetching', 'ready', 'error')),
            enabled boolean NOT NULL DEFAULT false,
            total_estimate integer NOT NULL DEFAULT 0,
            discovery_cursor text,
            last_history_id text,
            error text,
            started_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.gmail_index_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.gmail_index_state FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY gmail_index_state_owner ON app.gmail_index_state
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.gmail_index_state TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.gmail_index_state")
    op.execute("DROP TABLE app.gmail_message_meta")
