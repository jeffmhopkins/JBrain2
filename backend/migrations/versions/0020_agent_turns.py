"""Agent turns: the conversation transcript per session, so reopening Full Brain
replays the same exchange (text + the tool sources each turn surfaced).

Owner-only metadata, like the run log — a turn records what was asked and
answered for a session the owner owns, never in-scope note content (the sources
are pointers: note id + a denormalized snippet for render). RLS is the
is_owner() pattern. Cascades with its session so a deleted session takes its
transcript with it.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-12
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.agent_turns (
            id uuid PRIMARY KEY,
            seq bigint GENERATED ALWAYS AS IDENTITY,
            session_id uuid NOT NULL REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            run_id uuid REFERENCES app.agent_runs(id) ON DELETE SET NULL,
            role text NOT NULL CHECK (role IN ('user', 'assistant')),
            content text NOT NULL,
            -- Assistant turns carry the tool steps + their note sources (the
            -- "Worked" block); user turns carry [].
            tools jsonb NOT NULL DEFAULT '[]',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX agent_turns_session_idx ON app.agent_turns (session_id, seq)")

    op.execute("ALTER TABLE app.agent_turns ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.agent_turns FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY agent_turns_owner ON app.agent_turns
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT ON app.agent_turns TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.agent_turns")
