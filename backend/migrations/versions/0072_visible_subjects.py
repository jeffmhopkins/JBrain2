"""Member-visible subjects helper (JBrain360 M4b).

The member dashboard needs the subjects a viewer may see — itself plus its
family-group members — WITH their display labels, to populate the map's device
picker and the presence roster. Core `subjects` RLS is "owner OR your own
subject", so a device session can read only its own subject row and cannot
resolve a group member's name.

`app.visible_subjects(viewer)` answers that on the caller's behalf: SECURITY
DEFINER (owned by the migration superuser, so it bypasses the owner-only subjects
RLS), returning only `(id, display_name)` for the viewer's own subject and the
members who share a family group via `viewer_may_see`. It exposes labels only —
the location FIXES themselves stay gated by their own RLS (0067).

Revision ID: 0072
Revises: 0071
Create Date: 2026-06-18
"""

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.visible_subjects(viewer_subject text)
        RETURNS TABLE(subject_id uuid, display_name text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
          SELECT s.id, s.display_name
          FROM app.subjects s
          WHERE viewer_subject IS NOT NULL AND viewer_subject <> ''
            AND s.kind = 'device'
            AND (s.id::text = viewer_subject
                 OR app.viewer_may_see(viewer_subject, s.id::text))
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.visible_subjects(text)")
