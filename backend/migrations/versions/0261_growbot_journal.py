"""GrowBot diary: the creature's own brain log rows, kept on the box for a week.

docs/plans/GROWBOT_PLAN.md §2 "Brain log". Upstream's creature posts its diary (a rolling
window of inputs, goals and actions, human speech already hashed) to `/api/journal` every few
seconds; the sidecar used to answer and drop it. The owner asked (2026-11-12) for a brain log
he can hand the upstream developer, and this diary is the format that developer debugs from,
so the sidecar now forwards its rows here.

A snapshot resends the rows it sent before, so a row is kept once: `(sid, t, row_hash)` is
unique and the writer inserts with ON CONFLICT DO NOTHING. `head` is the page load that first
sent the row (build hash, deploy, mode), which is what joins a row to the code that wrote it.

Kept a week, pruned by the writer (`jbrain.growbot.journal`). Owner-only RLS like 0260's.

Revision ID: 0261
Revises: 0260
Create Date: 2026-11-12
"""

from alembic import op

revision = "0261"
down_revision = "0260"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_journal (
            id bigserial PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            persona_id uuid,
            robot text NOT NULL DEFAULT '',
            sid text NOT NULL,
            t bigint NOT NULL,
            row_hash text NOT NULL,
            event text NOT NULL DEFAULT '',
            row jsonb NOT NULL,
            head jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (sid, t, row_hash)
        )
        """
    )
    op.execute("CREATE INDEX growbot_journal_created_idx ON app.growbot_journal (created_at)")
    op.execute("ALTER TABLE app.growbot_journal ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.growbot_journal FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY growbot_journal_owner ON app.growbot_journal
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON app.growbot_journal TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.growbot_journal_id_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.growbot_journal")
