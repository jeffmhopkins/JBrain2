"""GrowBot: the desk robots' device keys, and the one-time tickets that open the creature.

docs/plans/GROWBOT_PLAN.md §2 "Access" (wave G1). `growbot_robots` holds one row per pairing
code with the hash of the device key the chip presents on the relay (`jbrain.auth.keys`
`hash_token`; the key itself is shown to the owner once and never stored). `growbot_tickets`
holds the hash of each short-lived ticket the owner mints to open GrowBot on a phone; the
sidecar redeems it once, before `expires_at`, by stamping `used_at`.

Both are owner-only, like `jshare_items`. The sidecar's `/internal/growbot/*` calls run under
the system owner context (as the wall's `/internal/pet` routes do), after its own
`growbot.token` has been checked; no share, member, device or capability session can read or
write either table.

Revision ID: 0254
Revises: 0253
Create Date: 2026-11-10
"""

from alembic import op

revision = "0254"
down_revision = "0253"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_robots (
            code text PRIMARY KEY CHECK (code ~ '^gb-[A-Za-z0-9]{1,32}$'),
            key_hash text NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            rotated_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.growbot_tickets (
            ticket_hash text PRIMARY KEY CHECK (ticket_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at timestamptz
        )
        """
    )
    for table in ("growbot_robots", "growbot_tickets"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_owner ON app.{table}
            FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.growbot_tickets")
    op.execute("DROP TABLE app.growbot_robots")
