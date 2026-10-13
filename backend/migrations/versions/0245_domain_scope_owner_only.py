"""A non-owner's domain_scopes GUC grants nothing on its own (SECURITY round 2).

`app.has_domain_scope(code)` had two arms: an owner arm (full owner, or a narrowed owner
holding `code`) and a bare `code = ANY(app.domain_scopes)` arm that held for ANY principal
kind. `device_context` stamps a member phone with `domain_scopes = 'location'`, so a
device session passed every policy guarded only by `has_domain_scope` — full read/write on
every location-domain note, chunk, attachment, entity, fact, event and review item. No
device query reaches those tables today, so it was latent, but the firewall was the SQL a
route happened to run, not Postgres.

The scope arm is now owner-only. A non-owner kind's scope claim counts only where a policy
names that kind, through `app.has_kind_scope(kind, code)`, so each grant is explicit:

- `device_key` + `location`: the tables a phone's fixed queries touch (its fixes, the
  view-scope read of its family's fixes, geofence state + fences, shared places, its FCM
  token, the who-saw-whom audit). The subject / principal pins already in each policy are
  unchanged and still load-bearing. `events` gains a SELECT-only arm limited to
  geofence-transition rows at a shared place about the device's own subject or one it may
  see — the member timeline, which used to rely on its own SQL as the firewall. A panel is also a
  `device_key` but carries no scope (`panel_context`), so it earns none of these.
- `jmolt_observer` + `jmolt`: the five `jmolt_*_read` SELECT policies (M19's read-only
  observer, which is deliberately not an owner so the outbox advance/purge stay shut).

Every other context that sets `domain_scopes` is an owner (narrowed or not), whose
behaviour is unchanged.

Revision ID: 0245
Revises: 0244
Create Date: 2026-10-13
"""

from alembic import op

revision = "0245"
down_revision = "0244"
branch_labels = None
depends_on = None

_SCOPES = "string_to_array(coalesce(current_setting('app.domain_scopes', true), ''), ',')"

_OWNER_ONLY = f"""
CREATE OR REPLACE FUNCTION app.has_domain_scope(code text) RETURNS boolean
LANGUAGE sql STABLE AS
$$
  SELECT app.is_owner()
     AND (
       coalesce(current_setting('app.owner_scoped', true), '') <> 'true'
       OR code = ANY({_SCOPES})
     )
$$
"""

# 0015's definition, restored on downgrade.
_ANY_KIND = f"""
CREATE OR REPLACE FUNCTION app.has_domain_scope(code text) RETURNS boolean
LANGUAGE sql STABLE AS
$$
  SELECT (
      app.is_owner()
      AND coalesce(current_setting('app.owner_scoped', true), '') <> 'true'
  )
  OR code = ANY({_SCOPES})
$$
"""

_KIND_SCOPE = f"""
CREATE FUNCTION app.has_kind_scope(kind text, code text) RETURNS boolean
LANGUAGE sql STABLE AS
$$
  SELECT current_setting('app.principal_kind', true) = kind
     AND code = ANY({_SCOPES})
$$
"""

_DEV = "app.has_kind_scope('device_key', domain_code)"
_HAS = "app.has_domain_scope(domain_code)"
_PIN = "subject_id::text = current_setting('app.subject_id', true)"
_IS_DEVICE = "current_setting('app.principal_kind', true) = 'device_key'"


def _scope(device: bool) -> str:
    return f"({_HAS} OR {_DEV})" if device else _HAS


def _policies(device: bool) -> list[str]:
    """The location policies, with (`device`) or without the explicit device arm. Only
    the scope term changes; each policy's pins are verbatim from 0061-0243."""
    s = _scope(device)
    owner_or_pin = f"(app.is_full_owner() OR {_PIN})"
    return [
        f"ALTER POLICY location_fixes_access ON app.location_fixes"
        f" USING ({s} AND {owner_or_pin}) WITH CHECK ({s} AND {owner_or_pin})",
        f"ALTER POLICY location_fixes_view_scope ON app.location_fixes USING ({s}"
        " AND app.viewer_may_see(current_setting('app.subject_id', true), subject_id::text))",
        f"ALTER POLICY geofence_state_access ON app.geofence_state"
        f" USING ({s} AND {owner_or_pin}) WITH CHECK ({s} AND {owner_or_pin})",
        f"ALTER POLICY place_geofence_read ON app.place_geofence USING ({s}"
        f" AND (app.is_full_owner() OR ({_IS_DEVICE} AND (subject_id IS NULL OR {_PIN}))))",
        f"ALTER POLICY place_share_read ON app.place_share USING ({s})",
        f"ALTER POLICY fcm_token_rw ON app.fcm_token USING ({s} AND (app.is_full_owner()"
        f" OR ({_IS_DEVICE}"
        " AND principal_id::text = current_setting('app.principal_id', true))))"
        f" WITH CHECK ({s} AND (app.is_full_owner() OR ({_IS_DEVICE}"
        " AND principal_id::text = current_setting('app.principal_id', true)"
        f" AND {_PIN})))",
        f"ALTER POLICY view_audit_read ON app.view_audit USING ({s} AND (app.is_full_owner()"
        " OR target_subject_id::text = current_setting('app.subject_id', true)"
        " OR viewer_subject_id::text = current_setting('app.subject_id', true)))",
        f"ALTER POLICY view_audit_write ON app.view_audit WITH CHECK ({s}"
        f" AND (app.is_full_owner() OR ({_IS_DEVICE}"
        " AND viewer_subject_id::text = current_setting('app.subject_id', true)"
        " AND (target_subject_id = viewer_subject_id"
        " OR app.viewer_may_see(viewer_subject_id::text, target_subject_id::text)))))",
    ]


# The member timeline's one read of `events`: geofence crossings at an owner-SHARED place
# about the device's own subject or a family member it may see. The place match compares
# text, never casts, so a malformed payload id just fails to match. `place_share` is read
# through the device's own arm on it. No write arm — transitions are emitted under the
# system context (locations/geofence.py).
_EVENTS_DEVICE = f"""
CREATE POLICY events_device_geofence_read ON app.events FOR SELECT
USING (
    {_DEV}
    AND type = 'location.geofence_transition'
    AND (
        payload->>'subject_id' = current_setting('app.subject_id', true)
        OR app.viewer_may_see(current_setting('app.subject_id', true), payload->>'subject_id')
    )
    AND EXISTS (
        SELECT 1 FROM app.place_share ps
        WHERE ps.place_entity_id::text = payload->>'place_entity_id'
    )
)
"""

_JMOLT_READS = (
    ("jmolt_action_ledger_read", "jmolt_action_ledger"),
    ("jmolt_journal_read", "jmolt_journal"),
    ("jmolt_outbox_read", "jmolt_outbox"),
    ("jmolt_scratch_read", "jmolt_scratch"),
    ("jmolt_scratch_archive_read", "jmolt_scratch_archive"),
)


def upgrade() -> None:
    op.execute(_KIND_SCOPE)
    # Add every explicit arm before narrowing the function, so no grant lapses mid-way.
    for sql in _policies(device=True):
        op.execute(sql)
    op.execute(_EVENTS_DEVICE)
    for name, table in _JMOLT_READS:
        op.execute(
            f"ALTER POLICY {name} ON app.{table} USING (app.has_domain_scope('jmolt')"
            " OR app.has_kind_scope('jmolt_observer', 'jmolt'))"
        )
    op.execute(_OWNER_ONLY)


def downgrade() -> None:
    op.execute(_ANY_KIND)
    for name, table in _JMOLT_READS:
        op.execute(f"ALTER POLICY {name} ON app.{table} USING (app.has_domain_scope('jmolt'))")
    op.execute("DROP POLICY events_device_geofence_read ON app.events")
    for sql in _policies(device=False):
        op.execute(sql)
    op.execute("DROP FUNCTION app.has_kind_scope(text, text)")
