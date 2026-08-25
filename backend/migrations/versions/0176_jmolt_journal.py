"""jmolt's journal: an append-only line from jmolt TO its human (docs/plans/JMOLT_PLAN.md).

Each night jmolt may leave a short journal entry — what it did, what it is thinking about,
what it wants — surfaced to the owner in the morning digest and the PWA. It is jmolt's
voice, not a system log: append-only, jmolt-authored, never edited by anyone.

RLS is the same M19 split as the scratchpad (0173): jmolt and jerv both run as the owner
principal, so `is_owner()` cannot tell them apart. So:

- **SELECT** is gated on `has_domain_scope('jmolt')` — jmolt's nightly session, jerv's
  narrowed observation session, and the unrestricted owner may read; a session in neither
  domain sees nothing.
- **INSERT** is pinned to `auth_ctx() = 'jmolt'` AND jmolt's own principal id — only
  jmolt's own nightly launcher writes, and only its own rows (M19(d)).
- There is **no UPDATE** grant or policy at all: an entry, once written, is immutable — the
  human corrects the record with their own note, never by rewriting jmolt's words.

A bounded retention prune (DELETE under jmolt's auth context) keeps the table from growing
without limit, exactly like the scratch archive; no journal tool issues a DELETE, so
jmolt-the-agent can only append.
"""

from alembic import op

revision = "0176"
down_revision = "0175"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jmolt_journal (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            content text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX jmolt_journal_idx ON app.jmolt_journal (principal_id, created_at DESC)"
    )

    op.execute("ALTER TABLE app.jmolt_journal ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jmolt_journal FORCE ROW LEVEL SECURITY")
    # Read: anyone holding the jmolt domain scope (jmolt, jerv's observation, the owner).
    op.execute(
        """
        CREATE POLICY jmolt_journal_read ON app.jmolt_journal
        FOR SELECT USING (app.has_domain_scope('jmolt'))
        """
    )
    # Append: only under jmolt's own auth context, and only jmolt's own rows (M19(d)).
    op.execute(
        """
        CREATE POLICY jmolt_journal_append ON app.jmolt_journal
        FOR INSERT WITH CHECK (
            app.auth_ctx() = 'jmolt'
            AND principal_id = current_setting('app.principal_id', true)
        )
        """
    )
    # The bounded retention prune (repo write path) deletes under jmolt's auth context.
    op.execute(
        """
        CREATE POLICY jmolt_journal_prune ON app.jmolt_journal
        FOR DELETE USING (app.auth_ctx() = 'jmolt')
        """
    )

    # No UPDATE grant: journal entries are immutable once written.
    op.execute("GRANT SELECT, INSERT, DELETE ON app.jmolt_journal TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.jmolt_journal_seq_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jmolt_journal")
