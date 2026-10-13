"""Pin the view-scope helpers and the who-saw-whom write to the caller (SECURITY S5, F16).

Both SECURITY DEFINER helpers took the viewer as an argument and trusted it:
`app.viewer_may_see(viewer, target)` (0067) and `app.visible_subjects(viewer)` (0072).
Any session could ask "may <someone else> see <x>?" or list another member's family
roster with names — membership the owner-only `view_scope` is meant to keep private. And
`view_audit_write` (0069) let a device write "<me> viewed <x>" about any subject at all,
visible or not. No route does any of this today (every caller passes its own subject, and
S3.4 audits only visible subjects), so the pin guards against an app bug that passes the
wrong viewer argument, not against arbitrary SQL — a session that can run any statement can
also set its own GUCs, and the debug console runs as full owner, which is exempt anyway.

The pin: an owner session that holds the location domain (`app.is_owner()` AND
`app.has_domain_scope('location')` — the full owner, the worker's context that push routing
runs under, a location-narrowed agent) may pass any viewer; anyone else only its own
`app.subject_id`, otherwise false / no rows. A narrowed owner without location gets nothing:
`view_scope` itself is location-firewalled, so its membership must not leak around that
through these functions. False rather than an error so a caller that
gets it wrong degrades to "can't see", never a 500 — and the `location_fixes` view-scope
policy, which already passes `current_setting('app.subject_id')`, is unaffected. The live
path's `SqlViewScopeRepo.may_view` used to ask from an empty context; it now asks under
the viewer's `device_context` (same change), or the member live map would go blank.

`current_setting` inside a SECURITY DEFINER function still reads the caller's
transaction GUCs — definer changes the role, not the settings — which is what makes the
pin possible here.

The audit write adds visibility: a device's row must be about its own subject or one it
may see. Its own subject is spelled out because `viewer_may_see(self, self)` is false
for a member in no family group, and the live socket audits a member's own track.

Revision ID: 0242
Revises: 0241
Create Date: 2026-10-12
"""

from alembic import op

revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None

_VIEWER_MAY_SEE = """
CREATE OR REPLACE FUNCTION app.viewer_may_see(viewer_subject text, target_subject text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT viewer_subject IS NOT NULL AND viewer_subject <> ''
     {pin}
     AND EXISTS (
       SELECT 1
       FROM app.view_scope v
       JOIN app.view_scope t ON t.group_id = v.group_id
       WHERE v.member_subject_id::text = viewer_subject
         AND t.member_subject_id::text = target_subject
     )
$$
"""

_VISIBLE_SUBJECTS = """
CREATE OR REPLACE FUNCTION app.visible_subjects(viewer_subject text)
RETURNS TABLE(subject_id uuid, display_name text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT s.id, s.display_name
  FROM app.subjects s
  WHERE viewer_subject IS NOT NULL AND viewer_subject <> ''
    {pin}
    AND s.kind = 'device'
    AND (s.id::text = viewer_subject
         OR app.viewer_may_see(viewer_subject, s.id::text))
$$
"""

_PIN = (
    "AND ((app.is_owner() AND app.has_domain_scope('location'))"
    " OR viewer_subject = coalesce(current_setting('app.subject_id', true), ''))"
)

_AUDIT_WRITE = """
CREATE POLICY view_audit_write ON app.view_audit FOR INSERT
WITH CHECK (
    app.has_domain_scope(domain_code)
    AND (
        app.is_full_owner()
        OR (
            current_setting('app.principal_kind', true) = 'device_key'
            AND viewer_subject_id::text = current_setting('app.subject_id', true)
            {visible}
        )
    )
)
"""

_VISIBLE = (
    "AND (target_subject_id = viewer_subject_id"
    " OR app.viewer_may_see(viewer_subject_id::text, target_subject_id::text))"
)


def upgrade() -> None:
    op.execute(_VIEWER_MAY_SEE.format(pin=_PIN))
    op.execute(_VISIBLE_SUBJECTS.format(pin=_PIN))
    op.execute("DROP POLICY view_audit_write ON app.view_audit")
    op.execute(_AUDIT_WRITE.format(visible=_VISIBLE))


def downgrade() -> None:
    op.execute("DROP POLICY view_audit_write ON app.view_audit")
    op.execute(_AUDIT_WRITE.format(visible=""))
    op.execute(_VISIBLE_SUBJECTS.format(pin=""))
    op.execute(_VIEWER_MAY_SEE.format(pin=""))
