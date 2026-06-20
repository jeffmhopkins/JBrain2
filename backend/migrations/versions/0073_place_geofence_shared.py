"""`place_share` — the owner's per-place member-visibility opt-in (JBrain360 M4c).

The member dashboard shows positions for the whole family group, but place *names*
and the geofence overlay are owner-private by default: a fence named "Therapist"
must not leak to members. A row in `place_share` is the owner's explicit opt-in —
only shared places appear in a member's fence overlay, and only shared-place
crossings appear in a member's timeline (owner decision: per-place opt-in). The
owner's own `/locations` reads are unaffected (they see every fence).

Kept in its OWN table, NOT a column on `place_geofence`: that mirror is
projector-owned and rebuilt **delete-then-insert** on every re-analysis / sweep
(geofence_projection._project_one), which would silently reset a flag living on
it. `place_share` is keyed by the stable `place_entity_id` (cascading on entity
purge — the privacy promise that nothing derived from a deleted Place survives)
and is never touched by the projector, so the owner's choice persists.

RLS: a row is the shared SET, which is member-visible by definition — any
location-scoped principal may READ it (so a member's overlay/timeline query can
join it); only a full owner may WRITE (toggle sharing). It carries no name or
geometry, so a device learns only "place X is shared", which is exactly what
shared means.

Revision ID: 0073
Revises: 0072
Create Date: 2026-06-20
"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.place_share (
            place_entity_id uuid PRIMARY KEY REFERENCES app.entities(id) ON DELETE CASCADE,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            shared_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.place_share ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.place_share FORCE ROW LEVEL SECURITY")
    # Read: any location-scoped principal (owner + member device) — the shared set
    # is member-visible by definition. Write: full owner / system only.
    op.execute(
        "CREATE POLICY place_share_read ON app.place_share FOR SELECT"
        " USING (app.has_domain_scope(domain_code))"
    )
    op.execute(
        "CREATE POLICY place_share_write ON app.place_share FOR ALL"
        " USING (app.has_domain_scope(domain_code) AND app.is_full_owner())"
        " WITH CHECK (app.has_domain_scope(domain_code) AND app.is_full_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.place_share TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.place_share")
