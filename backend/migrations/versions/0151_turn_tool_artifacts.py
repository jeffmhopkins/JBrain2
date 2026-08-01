"""`turn_tool_artifacts` — expensive/large tool results jerv can reference across turns.

A jerv tool result (a fetched web page, a YouTube caption transcript) is discarded at
turn's end today: chat history carries only text, so a follow-up turn re-fetches from
scratch and loses its paging place (docs/plans/CROSS_TURN_TOOL_RESULTS_PLAN.md). This is
the by-reference store that fixes that — modeled on `turn_attachments` (0074): the heavy
text lives content-addressed in the blob store (`sha256`), the row holds only the metadata
+ the paging cursor, and each later turn re-injects a compact DATA-framed reference line so
the model can continue the artifact with `read_artifact` instead of re-fetching.

`domain_code` carries the firewall scope, stamped from the session's scopes at creation
exactly like a chat attachment (a single-domain session stamps that domain; a multi/all/
empty-scope session — jerv is empty-scoped — stamps 'general'), so the standard
`has_domain_scope` policy needs no join. `session_id` cascades; `turn_id` is nullable
(an artifact is created DURING the assistant turn, session-scoped — turn binding is
optional provenance). `(session_id, source_url)` is unique so a re-fetch of the same URL
this session refreshes the one row rather than piling up.

Revision ID: 0151
Revises: 0150
Create Date: 2026-08-01
"""

from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.turn_tool_artifacts (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL
                REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            -- Optional provenance: SET NULL keeps the artifact (and its blob link) if
            -- the turn is later deleted. An artifact is session-scoped; the reference
            -- injection reads by session, so binding is not required for the feature.
            turn_id uuid REFERENCES app.agent_turns(id) ON DELETE SET NULL,
            domain_code text NOT NULL REFERENCES app.domains(code),
            -- The tool class that produced it (web_fetch | youtube | ...), so read_artifact
            -- renders it right and the reference line reads naturally.
            kind text NOT NULL,
            -- The URL (or ref) the artifact was fetched from — the per-session dedup key.
            source_url text NOT NULL,
            title text NOT NULL DEFAULT '',
            -- Content-addressed pointer to the FULL text in the blob store (the heavy
            -- payload stays out of the row, invariant #2).
            sha256 text NOT NULL,
            -- Length of the full text, and the paging cursor read_artifact advances so a
            -- "continue where I left off" read resumes across turns.
            total_chars bigint NOT NULL DEFAULT 0,
            last_offset bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX turn_tool_artifacts_session_idx ON app.turn_tool_artifacts (session_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX turn_tool_artifacts_session_url_idx"
        " ON app.turn_tool_artifacts (session_id, source_url)"
    )
    op.execute("ALTER TABLE app.turn_tool_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.turn_tool_artifacts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY turn_tool_artifacts_domain ON app.turn_tool_artifacts
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.turn_tool_artifacts TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.turn_tool_artifacts")
