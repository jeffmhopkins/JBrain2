"""Retire the keys thirteen flashes left behind, and the unit the owner is finished with.

ONE-TIME CLEANUP OF A PILE THAT CAN NO LONGER FORM. `/flash` minted a fresh identity every time
it ran and nothing retired the old one, so by tonight one panel on one wall was thirteen live
device keys under one name — every one of them still authenticating, and the owner's device list
thirteen identical rows deep, which is how the two keys he actually needed to revoke came to be
buried. 0211 made the flash rotate instead, so the pile stops growing; this clears what it
already grew.

**Newest per panel survives, and that is safe rather than merely tidy.** A flash rewrites the
unit's NVS with the key it just minted, so for a given name the newest principal IS the one that
panel is using and every older one is dead by construction. No liveness signal is needed to know
that — which matters, because the obvious alternative (keep what has reported telemetry) would
have revoked the ONLY key of a panel flashed two hours ago that has not yet come up, and
de-authorising a working unit for being quiet is the exact mistake this change exists to stop
repeating.

**The unnamed unit is retired whole, and that is a decision rather than a rule.** A third board
was flashed without a name, took the `room endpoint panel` default, and by matching the old
roster predicate put itself into two children's addressing — three panels where `send(to="panel")`
needs two, so the twins' voice messages stopped. The owner has said twice that it is finished
with and destined for a different project. It is named here explicitly rather than by a clever
predicate, because "retire panels that have no name" as a standing rule would revoke the first
key of every future unnamed flash the moment it enrolled.

Migrations run once, so this is a cleanup and not a policy. Nothing here touches a phone:
`device_role IS NOT NULL` is the panel filter, and the owner's own device key has no role.

Revision ID: 0213
Revises: 0212
Create Date: 2026-09-24
"""

from alembic import op

revision = "0213"
down_revision = "0212"
branch_labels = None
depends_on = None

# `endpoint.UNNAMED_PANEL_LABEL`. Restated rather than imported: a migration is a historical
# record and must keep meaning what it meant, even if that constant is later renamed.
UNNAMED = "room endpoint panel"


def upgrade() -> None:
    # 1. SUPERSEDED KEYS. Everything but the newest, per panel name and role.
    op.execute(
        """
        UPDATE app.principals p SET revoked_at = now()
        WHERE p.kind = 'device_key' AND p.revoked_at IS NULL
          AND p.id IN (
              SELECT id FROM (
                  SELECT pr.id,
                         row_number() OVER (
                             PARTITION BY s.display_name, s.device_role
                             ORDER BY pr.created_at DESC
                         ) AS rn
                  FROM app.principals pr
                  JOIN app.subjects s ON s.id = pr.subject_id
                  WHERE pr.kind = 'device_key' AND pr.revoked_at IS NULL
                    AND s.device_role IS NOT NULL
              ) ranked
              WHERE rn > 1
          )
        """
    )

    # 2. THE UNNAMED UNIT, whole. Both keys if both are still live — the step above may have
    # already taken one of them, and this is deliberately not conditional on that.
    op.execute(
        """
        UPDATE app.principals p SET revoked_at = now()
        FROM app.subjects s
        WHERE s.id = p.subject_id AND p.kind = 'device_key' AND p.revoked_at IS NULL
          AND s.device_role IS NOT NULL AND s.display_name = :unnamed
        """.replace(":unnamed", f"'{UNNAMED}'")
    )


def downgrade() -> None:
    # DELIBERATELY NOT REVERSIBLE. Un-revoking would hand working credentials back to boards that
    # may have been re-flashed, handed on, or repurposed since — and a downgrade that silently
    # re-authorises hardware is worse than one that does nothing.
    pass
