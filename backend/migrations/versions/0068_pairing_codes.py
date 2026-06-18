"""Pairing codes + atomic redemption (JBrain360 M2c).

Onboarding: the owner mints a short-lived, one-time `pairing_code`; a family
member's phone redeems it to receive a device key + its OwnTracks config, without
the owner ever copying a key by hand.

Redemption is unauthenticated — the device has no principal *yet* — so it cannot
run under an owner session. Rather than open the owner-only identity tables to a
redemption context, the work is one **SECURITY DEFINER** function,
`app.redeem_pairing_code`: it validates the code (unredeemed, unexpired, locked
`FOR UPDATE` so a concurrent double-redeem loses the re-check), creates the device
subject + `device_key` principal, marks the code redeemed, and returns the new
identity — all atomically, bypassing RLS as the migration superuser owner. The
plaintext key never touches the DB: the caller passes only its hash.

The `pairing_code` table itself is owner-only (is_full_owner), like `family_group`.

Revision ID: 0068
Revises: 0067
Create Date: 2026-06-18
"""

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.pairing_code (
            code text PRIMARY KEY,
            label text NOT NULL,
            monitoring int NOT NULL DEFAULT 1,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            expires_at timestamptz NOT NULL,
            redeemed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.pairing_code ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.pairing_code FORCE ROW LEVEL SECURITY")
    # Owner-only: a full owner mints/lists/revokes codes; nobody else sees them.
    # Redemption does NOT read the table directly — it goes through the function.
    op.execute(
        "CREATE POLICY pairing_code_owner ON app.pairing_code FOR ALL"
        " USING (app.has_domain_scope(domain_code) AND app.is_full_owner())"
        " WITH CHECK (app.has_domain_scope(domain_code) AND app.is_full_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.pairing_code TO jbrain_app")

    # Atomic redemption. Returns the new device identity, or no rows when the code
    # is invalid / expired / already redeemed. SECURITY DEFINER (superuser owner) so
    # it may create identity rows the RLS-bound app role cannot.
    op.execute(
        """
        CREATE FUNCTION app.redeem_pairing_code(p_code text, p_key_hash text)
        RETURNS TABLE(subject_id uuid, principal_id uuid, label text, monitoring int)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
          v_label text;
          v_mon   int;
          v_sid   uuid := gen_random_uuid();
          v_pid   uuid := gen_random_uuid();
        BEGIN
          SELECT pc.label, pc.monitoring INTO v_label, v_mon
            FROM app.pairing_code pc
           WHERE pc.code = p_code
             AND pc.redeemed_at IS NULL
             AND pc.expires_at > now()
           FOR UPDATE;
          IF NOT FOUND THEN
            RETURN;  -- invalid / expired / already redeemed
          END IF;
          INSERT INTO app.subjects (id, display_name, kind)
            VALUES (v_sid, v_label, 'device');
          INSERT INTO app.principals (id, kind, subject_id, key_hash, label)
            VALUES (v_pid, 'device_key', v_sid, p_key_hash, v_label);
          UPDATE app.pairing_code SET redeemed_at = now() WHERE code = p_code;
          RETURN QUERY SELECT v_sid, v_pid, v_label, v_mon;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.redeem_pairing_code(text, text)")
    op.execute("DROP TABLE IF EXISTS app.pairing_code")
