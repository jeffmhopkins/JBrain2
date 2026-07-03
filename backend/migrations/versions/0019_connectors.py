"""The egress chokepoint's stores: cached external reference data and the egress
audit log (docs/reference/ASSISTANT.md "External connectors", invariant #9).

The only outbound egress is the connector abstraction — a fixed allowlist of named,
server-side, owner-configured upstreams called with typed minimal inputs, cached,
and logged. Both tables are owner-only and domain-firewalled: the location cache is
location-scoped, so a non-location session can't read it. The log records the
connector, a hash of the normalized input, the domain, and the principal — never
the payload.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-12
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.connector_cache (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            connector text NOT NULL,
            input_hash text NOT NULL,
            result jsonb NOT NULL,
            domain_code text NOT NULL REFERENCES app.domains(code),
            fetched_at timestamptz NOT NULL DEFAULT now(),
            ttl_seconds int NOT NULL DEFAULT 86400,
            UNIQUE (connector, input_hash)
        )
        """
    )
    op.execute("ALTER TABLE app.connector_cache ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.connector_cache FORCE ROW LEVEL SECURITY")
    # Owner-only AND domain-narrowed: reference data is cached per the domain it
    # serves; the location cache is location-scoped (#9).
    op.execute(
        """
        CREATE POLICY connector_cache_owner ON app.connector_cache
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.connector_cache TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.connector_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            connector text NOT NULL,
            input_hash text NOT NULL,
            domain_code text NOT NULL REFERENCES app.domains(code),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX connector_log_at_idx ON app.connector_log (at DESC)")
    op.execute("ALTER TABLE app.connector_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.connector_log FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY connector_log_owner ON app.connector_log
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT ON app.connector_log TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.connector_log")
    op.execute("DROP TABLE app.connector_cache")
