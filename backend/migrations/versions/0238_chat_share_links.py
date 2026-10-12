"""Public share links for agent chats: a revocable, no-login read of one whole conversation.

The owner mints a link from a chat's swipe rail; anyone with it reads that chat's full
transcript (answers, reasoning, tool steps) as it stands at the moment they open it, so turns
added later appear on their next load. A plain token-in-URL bearer read: only the token's
SHA-256 is stored, there is no expiry, and `revoked_at` kills it. Mirrors the research (0150),
jlaunch (0153) and map (0230) shares.

The capability is enforced in RLS. A visitor runs under `chat_share_context`, a non-owner,
empty-scope context carrying `auth_context = 'chat_share'` and, once the token resolves,
`principal_id = <link id>`. The three `*_share` policies below are the ONLY grants it earns:
each joins the pinned, unrevoked link to its one session, so a link reads its own chat's
session row, turns and runs and nothing else — not another chat, not a sub-agent child, not a
note, not a chat attachment (those stay behind the domain firewall). The runs grant exists for
one column: a turn's elapsed time is its run's start to its record, the bubble's "Thought for".
The link id is compared as text, never cast, so an empty pin fails closed instead of throwing.

Revision ID: 0238
Revises: 0237
Create Date: 2026-10-12
"""

from alembic import op

revision = "0238"
down_revision = "0237"
branch_labels = None
depends_on = None

_LINK_LIVE = """
    l.id::text = current_setting('app.principal_id', true)
    AND l.revoked_at IS NULL
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.chat_share_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Only the SHA-256 of the 256-bit bearer secret; the secret is shown once at
            -- mint and never recoverable.
            token_hash text NOT NULL UNIQUE,
            -- A deleted chat takes its links with it.
            session_id uuid NOT NULL REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            last_viewed_at timestamptz,
            view_count integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("CREATE INDEX chat_share_links_session_idx ON app.chat_share_links (session_id)")
    op.execute("ALTER TABLE app.chat_share_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.chat_share_links FORCE ROW LEVEL SECURITY")
    # The owner owns the lifecycle (mint / list / revoke) and the SYSTEM view-count bump runs
    # as owner. A visitor may only SELECT: resolve a token, and satisfy the subqueries below.
    op.execute(
        """
        CREATE POLICY chat_share_links_owner ON app.chat_share_links
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY chat_share_links_public_read ON app.chat_share_links
        FOR SELECT USING (app.auth_ctx() = 'chat_share')
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.chat_share_links TO jbrain_app")

    # The keystones, permissive and SELECT-only: OR'd with the owner policies, which deny this
    # non-owner context, so for a visitor they are the sole path to a chat.
    op.execute(
        f"""
        CREATE POLICY agent_sessions_share ON app.agent_sessions FOR SELECT
        USING (
            app.auth_ctx() = 'chat_share'
            AND EXISTS (
                SELECT 1 FROM app.chat_share_links l
                WHERE {_LINK_LIVE} AND l.session_id = app.agent_sessions.id
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY agent_turns_share ON app.agent_turns FOR SELECT
        USING (
            app.auth_ctx() = 'chat_share'
            AND EXISTS (
                SELECT 1 FROM app.chat_share_links l
                WHERE {_LINK_LIVE} AND l.session_id = app.agent_turns.session_id
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY runs_chat_share ON app.runs FOR SELECT
        USING (
            app.auth_ctx() = 'chat_share'
            AND EXISTS (
                SELECT 1 FROM app.chat_share_links l
                WHERE {_LINK_LIVE} AND l.session_id = app.runs.session_id
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY runs_chat_share ON app.runs")
    op.execute("DROP POLICY agent_turns_share ON app.agent_turns")
    op.execute("DROP POLICY agent_sessions_share ON app.agent_sessions")
    op.execute("DROP TABLE app.chat_share_links")
