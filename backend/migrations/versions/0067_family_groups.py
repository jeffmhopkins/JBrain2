"""Family groups + the view-scope read extension (JBrain360 M2a).

The location firewall (0061) is "full owner OR your own subject". This adds the
*third* visibility path — family-sees-family — entirely inside Postgres RLS, so a
gateway/broker bug can never hand out a fix the database itself would refuse.

A `family_group` holds owner-managed membership (`view_scope`); two subjects in the
same group may **read** each other's location. The decision is `app.viewer_may_see`,
consulted by an additional **SELECT-only** policy on `location_fixes`. It is
additive: the shipped own-subject/owner policy — and its WITH CHECK — is untouched,
so a viewer reads a family member's track but can never write it (writes stay
subject-pinned).

`viewer_may_see` is SECURITY DEFINER because the location policy runs as the
RLS-bound app role, which by design cannot read the owner-only `view_scope`; the
function (owned by the migration superuser, so it bypasses RLS) answers membership
on the caller's behalf and returns only a boolean.

The group is conceptually cross-domain (location now, the M8 `usage` domain later),
but is stamped `location` for the v1 firewall and gated like every firewalled table.

Revision ID: 0067
Revises: 0066
Create Date: 2026-06-18
"""

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Owner-managed family group. Owner-only firewall: a full owner reads/writes; a
    # device or capability token sees zero rows and cannot write (is_full_owner).
    op.execute(
        """
        CREATE TABLE app.family_group (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Group membership: the (group, subject) pairs `viewer_may_see` joins on.
    op.execute(
        """
        CREATE TABLE app.view_scope (
            group_id uuid NOT NULL REFERENCES app.family_group(id) ON DELETE CASCADE,
            member_subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            added_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, member_subject_id)
        )
        """
    )
    op.execute("CREATE INDEX view_scope_member_idx ON app.view_scope (member_subject_id)")

    for table in ("family_group", "view_scope"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table} FOR ALL"
            " USING (app.has_domain_scope(domain_code) AND app.is_full_owner())"
            " WITH CHECK (app.has_domain_scope(domain_code) AND app.is_full_owner())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")

    # Do two subjects share a family group? Consulted by the location_fixes SELECT
    # policy below. SECURITY DEFINER so the RLS-bound app role gets a membership
    # answer without read access to the owner-only view_scope; returns only a bool.
    op.execute(
        """
        CREATE FUNCTION app.viewer_may_see(viewer_subject text, target_subject text)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
          SELECT viewer_subject IS NOT NULL AND viewer_subject <> ''
             AND EXISTS (
               SELECT 1
               FROM app.view_scope v
               JOIN app.view_scope t ON t.group_id = v.group_id
               WHERE v.member_subject_id::text = viewer_subject
                 AND t.member_subject_id::text = target_subject
             )
        $$
        """
    )

    # Additive READ path: a family member may SELECT another member's fixes. The
    # shipped own-subject/owner policy (FOR ALL) and its WITH CHECK are untouched, so
    # permissive policies OR for reads while writes stay subject-pinned.
    op.execute(
        """
        CREATE POLICY location_fixes_view_scope ON app.location_fixes FOR SELECT
        USING (
            app.has_domain_scope(domain_code)
            AND app.viewer_may_see(current_setting('app.subject_id', true), subject_id::text)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS location_fixes_view_scope ON app.location_fixes")
    op.execute("DROP FUNCTION IF EXISTS app.viewer_may_see(text, text)")
    op.execute("DROP TABLE IF EXISTS app.view_scope")
    op.execute("DROP TABLE IF EXISTS app.family_group")
