"""Re-pair an existing device: pairing codes may target an existing subject.

Phones now own location entirely (the manual OwnTracks "add device" path is
retired in the UI). A paired phone can only receive credentials by redeeming a
pairing code, so "roll the token" / "rotate the key" for a phone is the same
operation as **re-pair**: mint a fresh one-time code bound to the existing device
subject; on redemption the device's key rotates in place (old key revoked, new
key minted) while its identity — and its stored history — stay attached.

This adds a nullable `pairing_code.subject_id` and teaches the SECURITY DEFINER
`app.redeem_pairing_code` to branch: when the code targets an existing device, it
rotates that device's key (revoke active `device_key` principals, insert a new
one) instead of creating a fresh subject. A NULL target keeps the original
new-device behaviour, so first-time pairing is unchanged. A re-pair carries the
device's *current* `display_name` as the config/principal label, so a rename
since the original pairing is honoured. The target is validated to still be a
device subject; anything else fails closed like an invalid code (no oracle).

Revision ID: 0077
Revises: 0076
Create Date: 2026-06-20
"""

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


_REPAIR_AWARE_REDEEM = """
CREATE OR REPLACE FUNCTION app.redeem_pairing_code(p_code text, p_key_hash text)
RETURNS TABLE(subject_id uuid, principal_id uuid, label text, monitoring int)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_label  text;
  v_mon    int;
  v_target uuid;
  v_sid    uuid;
  v_pid    uuid := gen_random_uuid();
BEGIN
  SELECT pc.label, pc.monitoring, pc.subject_id INTO v_label, v_mon, v_target
    FROM app.pairing_code pc
   WHERE pc.code = p_code
     AND pc.redeemed_at IS NULL
     AND pc.expires_at > now()
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN;  -- invalid / expired / already redeemed
  END IF;

  IF v_target IS NOT NULL THEN
    -- Re-pair: rotate the key on the existing device, keeping its identity +
    -- history. A vanished or non-device target fails closed like a bad code.
    SELECT display_name INTO v_label
      FROM app.subjects WHERE id = v_target AND kind = 'device';
    IF NOT FOUND THEN
      RETURN;
    END IF;
    v_sid := v_target;
    -- Qualify the column: a bare `subject_id` would collide with this function's
    -- RETURNS TABLE OUT parameter of the same name (ambiguous reference).
    UPDATE app.principals SET revoked_at = now()
     WHERE principals.subject_id = v_sid
       AND principals.kind = 'device_key' AND principals.revoked_at IS NULL;
  ELSE
    v_sid := gen_random_uuid();
    INSERT INTO app.subjects (id, display_name, kind)
      VALUES (v_sid, v_label, 'device');
  END IF;

  INSERT INTO app.principals (id, kind, subject_id, key_hash, label)
    VALUES (v_pid, 'device_key', v_sid, p_key_hash, v_label);
  UPDATE app.pairing_code SET redeemed_at = now() WHERE code = p_code;
  RETURN QUERY SELECT v_sid, v_pid, v_label, v_mon;
END;
$$
"""

# The original (0068) new-device-only body, restored on downgrade.
_NEW_DEVICE_ONLY_REDEEM = """
CREATE OR REPLACE FUNCTION app.redeem_pairing_code(p_code text, p_key_hash text)
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
    RETURN;
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


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.pairing_code"
        " ADD COLUMN subject_id uuid REFERENCES app.subjects(id) ON DELETE CASCADE"
    )
    op.execute(_REPAIR_AWARE_REDEEM)

    # Deleting a device (subject + its key principals) is a new owner capability.
    # `subjects_access` is already FOR ALL (its USING covers DELETE), so subjects
    # just needs the table privilege; `principals` only had select/insert/update
    # policies, so it needs an owner-only DELETE policy too.
    op.execute("GRANT DELETE ON app.subjects TO jbrain_app")
    op.execute("GRANT DELETE ON app.principals TO jbrain_app")
    op.execute(
        "CREATE POLICY principals_delete ON app.principals FOR DELETE USING (app.is_owner())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS principals_delete ON app.principals")
    op.execute("REVOKE DELETE ON app.principals FROM jbrain_app")
    op.execute("REVOKE DELETE ON app.subjects FROM jbrain_app")
    op.execute(_NEW_DEVICE_ONLY_REDEEM)
    op.execute("ALTER TABLE app.pairing_code DROP COLUMN subject_id")
