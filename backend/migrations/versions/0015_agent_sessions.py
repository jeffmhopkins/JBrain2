"""Agent sessions + an owner-narrowable domain firewall.

An agent session is a capability: the owner picks which domains it may READ, and
that selection must be a real RLS firewall, not a soft filter the tools could be
talked out of. But the owner always passes `app.has_domain_scope` (is_owner()
short-circuits), so this migration makes the firewall honour an opt-in
`app.owner_scoped` GUC: when set to 'true', even the owner is restricted to
`app.domain_scopes`. It defaults off, so the worker and ordinary owner API
sessions keep seeing everything — fully backward compatible (docs/reference/ASSISTANT.md
"Session capabilities", invariant #4).

`agent_sessions` itself is owner-only metadata (not per-row domain content), so
its RLS is the `is_owner()` pattern, like `app.jobs` — and it stays visible to a
*narrowed* owner session, because owner_scoped only restricts domain data, never
owner identity.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-12
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# Owner is restricted to app.domain_scopes when app.owner_scoped='true'; otherwise
# is_owner() still grants everything (worker, ordinary owner sessions).
_NARROWED = """
CREATE OR REPLACE FUNCTION app.has_domain_scope(code text) RETURNS boolean
LANGUAGE sql STABLE AS
$$
  SELECT (
      app.is_owner()
      AND coalesce(current_setting('app.owner_scoped', true), '') <> 'true'
  )
  OR code = ANY(
    string_to_array(coalesce(current_setting('app.domain_scopes', true), ''), ',')
  )
$$
"""

# The pre-0015 definition, restored on downgrade.
_ORIGINAL = """
CREATE OR REPLACE FUNCTION app.has_domain_scope(code text) RETURNS boolean
LANGUAGE sql STABLE AS
$$
  SELECT app.is_owner() OR code = ANY(
    string_to_array(coalesce(current_setting('app.domain_scopes', true), ''), ',')
  )
$$
"""


def upgrade() -> None:
    op.execute(_NARROWED)
    op.execute(
        """
        CREATE TABLE app.agent_sessions (
            id uuid PRIMARY KEY,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            title text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
            domain_scopes text[] NOT NULL,
            subject_ids uuid[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            last_active_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX agent_sessions_active_idx ON app.agent_sessions (last_active_at DESC)")
    op.execute("ALTER TABLE app.agent_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.agent_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY agent_sessions_owner ON app.agent_sessions
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.agent_sessions TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.agent_sessions")
    op.execute(_ORIGINAL)
