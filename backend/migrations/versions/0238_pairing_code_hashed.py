"""Pairing codes at rest as sha256 (docs/plans/SECURITY_REVIEW_FIXES_PLAN.md S3.3, F12).

A pairing code is a bearer credential for fifteen minutes: whoever redeems it gets a device
key that outlives the code, the debug token that read it, and anything else that saw the
row. So the table keeps `sha256(code)` hex, and `locations/pairing.py` hashes before both
the INSERT and the call to `app.redeem_pairing_code` — the function compares
`code = p_code` and needs no change, it just meets a digest on both sides. Not
`auth.keys.hash_key`: that lowercases, and these codes are case-sensitive urlsafe base64.

A code minted before this migration is plaintext in the row and may be sitting in a QR on
someone's screen right now, so the rows are hashed in place and that QR still redeems. The
`!~` guard makes the UPDATE safe to meet a row that is already a digest (a plaintext code is
27 urlsafe chars, never 64 lowercase hex).

Revision ID: 0238
Revises: 0237
Create Date: 2026-10-12
"""

from alembic import op

revision = "0238"
down_revision = "0237"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE app.pairing_code
           SET code = encode(sha256(convert_to(code, 'UTF8')), 'hex')
         WHERE code !~ '^[0-9a-f]{64}$'
        """
    )


def downgrade() -> None:
    # Irreversible: a digest can't be turned back into its code. Every row is short-lived
    # (15 minutes) and the code that wrote a digest can only redeem by digest, so leaving
    # the rows as they are is the honest downgrade — an unredeemed one simply expires.
    pass
