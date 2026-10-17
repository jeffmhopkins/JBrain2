"""`owner_devices` — the phones enrolled for the owner tunnel, and sessions bound to one.

docs/plans/OWNER_TUNNEL_PLAN.md §3.4 (wave A3). One row per enrolled phone: its label, the
hash of its client key (`key_hash`, what Caddy's `public_key_sha256` placeholder sends as
`X-JBrain-Device`), the key itself (the gateway checks its signature in wave B), and when it
was enrolled, last seen and revoked. `device_sessions.device_id` binds an owner session to
one of these: such a session authenticates only on a request from that same phone.

RLS copies `device_sessions`: the owner, or the `login` context. The per-request device
check and enrollment run before any principal exists, so `login` must read, insert and
update here; nothing outside the auth code can open that context (CLAUDE.md #3). No DELETE:
a revoked phone stays on record.

Revision ID: 0251
Revises: 0250
Create Date: 2026-10-17
"""

from alembic import op

revision = "0251"
down_revision = "0250"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.owner_devices (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            label text NOT NULL CHECK (octet_length(label) BETWEEN 1 AND 80),
            key_hash text NOT NULL UNIQUE CHECK (key_hash ~ '^[0-9a-f]{64}$'),
            public_key bytea NOT NULL,
            enrolled_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz,
            last_ingress text,
            revoked_at timestamptz
        )
        """
    )
    op.execute("ALTER TABLE app.owner_devices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.owner_devices FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY owner_devices_select ON app.owner_devices FOR SELECT
        USING (app.is_owner() OR app.auth_ctx() = 'login')
        """
    )
    op.execute(
        """
        CREATE POLICY owner_devices_insert ON app.owner_devices FOR INSERT
        WITH CHECK (app.is_owner() OR app.auth_ctx() = 'login')
        """
    )
    op.execute(
        """
        CREATE POLICY owner_devices_update ON app.owner_devices FOR UPDATE
        USING (app.is_owner() OR app.auth_ctx() = 'login')
        WITH CHECK (app.is_owner() OR app.auth_ctx() = 'login')
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.owner_devices TO jbrain_app")
    op.execute(
        "ALTER TABLE app.device_sessions ADD COLUMN device_id uuid REFERENCES app.owner_devices(id)"
    )
    # Revoking a phone ends its sessions by this column.
    op.execute(
        "CREATE INDEX device_sessions_device_id_idx ON app.device_sessions (device_id)"
        " WHERE device_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.device_sessions DROP COLUMN device_id")
    op.execute("DROP TABLE app.owner_devices")
