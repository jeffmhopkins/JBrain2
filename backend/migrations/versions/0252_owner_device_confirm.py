"""`owner_devices.confirmed_at` — a new phone works only once the owner confirms it.

docs/plans/OWNER_TUNNEL_PLAN.md §3.5 (wave A5). The box's enrollment screen is readable by
everything in its host network namespace, so a phone that scanned the QR is not yet proof the
owner was there. A row starts pending; the owner confirms it from their own session after
comparing its words, and until then the device guard and the bound-session lookup refuse it.

The `login` context may write this table (enrollment and the per-request check run before
any principal exists), so RLS alone would let it confirm a phone, or do what amounts to it:
un-revoke one, or swap another key into a confirmed row. A trigger therefore holds `login` to
the two columns its code writes (`last_seen_at`, `last_ingress`), lets only an owner session
set `confirmed_at`, once, never at insert, and lets no one un-revoke a phone. No backfill: a
phone enrolled before this wave shows as waiting and is confirmed like any other.

Revision ID: 0252
Revises: 0251
Create Date: 2026-10-19
"""

from alembic import op

revision = "0252"
down_revision = "0251"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.owner_devices ADD COLUMN confirmed_at timestamptz")
    op.execute(
        """
        CREATE FUNCTION app.owner_devices_guard_writes() RETURNS trigger
        LANGUAGE plpgsql AS
        $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.confirmed_at IS NOT NULL THEN
              RAISE EXCEPTION 'a phone is enrolled unconfirmed' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
          END IF;
          IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
            RAISE EXCEPTION 'a revoked phone stays revoked' USING ERRCODE = '42501';
          END IF;
          IF NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at
             AND (OLD.confirmed_at IS NOT NULL OR NOT app.is_owner()) THEN
            RAISE EXCEPTION 'only the owner confirms a phone, once' USING ERRCODE = '42501';
          END IF;
          IF NOT app.is_owner()
             AND (NEW.id, NEW.label, NEW.key_hash, NEW.public_key, NEW.enrolled_at,
                  NEW.revoked_at)
                 IS DISTINCT FROM
                 (OLD.id, OLD.label, OLD.key_hash, OLD.public_key, OLD.enrolled_at,
                  OLD.revoked_at) THEN
            RAISE EXCEPTION 'only the owner changes a phone' USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER owner_devices_guard_writes
        BEFORE INSERT OR UPDATE ON app.owner_devices
        FOR EACH ROW EXECUTE FUNCTION app.owner_devices_guard_writes()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER owner_devices_guard_writes ON app.owner_devices")
    op.execute("DROP FUNCTION app.owner_devices_guard_writes()")
    op.execute("ALTER TABLE app.owner_devices DROP COLUMN confirmed_at")
