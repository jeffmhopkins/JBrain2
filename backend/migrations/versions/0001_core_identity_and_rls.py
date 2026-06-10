"""Core identity tables, domain firewalls, and row-level security.

The app role (jbrain_app) is created by the database init script in
production and by conftest in tests — it must exist before this migration
runs. All RLS policies key off transaction-local GUCs set by
jbrain.db.session.scoped_session; FORCE ROW LEVEL SECURITY ensures even the
table owner is policy-bound (superusers always bypass, which is why the
application never connects as one).

Revision ID: 0001
Revises:
Create Date: 2026-06-10
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")

    op.execute(
        """
        CREATE FUNCTION app.is_owner() RETURNS boolean
        LANGUAGE sql STABLE AS
        $$ SELECT current_setting('app.principal_kind', true) = 'owner' $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.auth_ctx() RETURNS text
        LANGUAGE sql STABLE AS
        $$ SELECT coalesce(current_setting('app.auth_context', true), '') $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.has_domain_scope(code text) RETURNS boolean
        LANGUAGE sql STABLE AS
        $$
          SELECT app.is_owner() OR code = ANY(
            string_to_array(coalesce(current_setting('app.domain_scopes', true), ''), ',')
          )
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE app.domains (
            id smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code text NOT NULL UNIQUE,
            name text NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.subjects (
            id uuid PRIMARY KEY,
            display_name text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('person', 'device')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.principals (
            id uuid PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN ('owner', 'capability_token', 'device_key')),
            subject_id uuid REFERENCES app.subjects(id),
            key_hash text NOT NULL UNIQUE,
            label text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.device_sessions (
            id uuid PRIMARY KEY,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            token_hash text NOT NULL UNIQUE,
            label text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        )
        """
    )

    op.execute(
        "INSERT INTO app.domains (code, name) VALUES"
        " ('general', 'General'), ('health', 'Health'),"
        " ('finance', 'Finance'), ('location', 'Location')"
    )

    for table in ("domains", "subjects", "principals", "device_sessions"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")

    # Domains are reference data: readable by every principal, writable by none
    # (seeds change only via migrations).
    op.execute("CREATE POLICY domains_read ON app.domains FOR SELECT USING (true)")

    op.execute(
        """
        CREATE POLICY subjects_access ON app.subjects
        USING (app.is_owner() OR id::text = current_setting('app.subject_id', true))
        WITH CHECK (app.is_owner())
        """
    )

    # 'login' may look up credentials; 'bootstrap' is the CLI creating or
    # rotating the owner principal. Both are reachable only from auth code.
    op.execute(
        """
        CREATE POLICY principals_select ON app.principals FOR SELECT
        USING (
            app.is_owner()
            OR id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() IN ('login', 'bootstrap')
        )
        """
    )
    op.execute(
        """
        CREATE POLICY principals_insert ON app.principals FOR INSERT
        WITH CHECK (app.is_owner() OR app.auth_ctx() = 'bootstrap')
        """
    )
    op.execute(
        """
        CREATE POLICY principals_update ON app.principals FOR UPDATE
        USING (app.is_owner() OR app.auth_ctx() = 'bootstrap')
        WITH CHECK (app.is_owner() OR app.auth_ctx() = 'bootstrap')
        """
    )

    op.execute(
        """
        CREATE POLICY device_sessions_select ON app.device_sessions FOR SELECT
        USING (
            app.is_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() = 'login'
        )
        """
    )
    op.execute(
        """
        CREATE POLICY device_sessions_insert ON app.device_sessions FOR INSERT
        WITH CHECK (app.is_owner() OR app.auth_ctx() = 'login')
        """
    )
    op.execute(
        """
        CREATE POLICY device_sessions_update ON app.device_sessions FOR UPDATE
        USING (
            app.is_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() = 'login'
        )
        WITH CHECK (
            app.is_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() = 'login'
        )
        """
    )

    op.execute("GRANT USAGE ON SCHEMA app TO jbrain_app")
    op.execute("GRANT SELECT ON app.domains TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.subjects TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.principals TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.device_sessions TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP SCHEMA app CASCADE")
