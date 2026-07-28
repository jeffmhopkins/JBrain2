"""Public share links for research reports: a revocable, no-login read grant.

The owner mints a link that targets EITHER one report OR a whole report folder
(`app.report_groups`), chosen at mint. Anyone with the link reads it (a plain
token-in-URL bearer read — only the token's SHA-256 hash is stored here); the owner
revokes it (`revoked_at`) to kill it. No expiry, no cookie, no session.

The capability is enforced in RLS, not app code. A public visitor runs under a
non-owner, EMPTY-scope context (`research_share_context`, db/session.py) carrying
`auth_context = 'research_share'` and, once the token resolves, `principal_id = <link
id>` as the pin. Empty domain scope is load-bearing: with an `external` scope the
existing `research_reports_domain` policy (permissive/OR-combined) would match every
report and the video corpus too, making this grant a no-op. So the ONLY thing that
lets a visitor read a report is `research_reports_share` below, which joins the pinned
link to the report (or to its folder, for a group link) and re-checks `revoked_at` —
so a visitor pinned to link A reads exactly link A's target, and a revoked link reads
nothing, at the data layer regardless of the SQL the endpoint runs.

The `report_groups` table is deliberately NOT opened to the share context (0149 keeps
it owner-only, unread even by jerv): a group link snapshots the folder NAME into
`label` at mint, while membership stays dynamic via the `group_id` join here.

Revision ID: 0150
Revises: 0149
Create Date: 2026-07-28
"""

from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.research_share_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Only the SHA-256 of the 256-bit bearer secret; the secret itself is
            -- shown once at mint and never recoverable.
            token_hash text NOT NULL UNIQUE,
            -- Owner-facing name; for a group link this snapshots the folder name at
            -- mint (report_groups stays private, so the public view reads it here).
            label text,
            target_kind text NOT NULL,
            report_id uuid REFERENCES app.research_reports(id) ON DELETE CASCADE,
            -- CASCADE (not the SET NULL folders use): the exactly-one CHECK forbids a
            -- both-null row, so a group link must die with its folder.
            group_id uuid REFERENCES app.report_groups(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            last_viewed_at timestamptz,
            view_count integer NOT NULL DEFAULT 0,
            CONSTRAINT research_share_links_target_kind
                CHECK (target_kind IN ('report', 'group')),
            -- Kind and the populated column agree, and exactly one target is set.
            CONSTRAINT research_share_links_one_target CHECK (
                (target_kind = 'report' AND report_id IS NOT NULL AND group_id IS NULL)
                OR (target_kind = 'group' AND group_id IS NOT NULL AND report_id IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX research_share_links_report_idx ON app.research_share_links (report_id)"
    )
    op.execute("CREATE INDEX research_share_links_group_idx ON app.research_share_links (group_id)")
    op.execute("ALTER TABLE app.research_share_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.research_share_links FORCE ROW LEVEL SECURITY")
    # Owner owns the link lifecycle (mint / list / revoke); the SYSTEM view-count bump
    # runs as owner too. A public visitor may only SELECT (resolve a token, and satisfy
    # the reports-policy subquery) — never write.
    op.execute(
        """
        CREATE POLICY research_share_links_owner ON app.research_share_links
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY research_share_links_public_read ON app.research_share_links
        FOR SELECT USING (app.auth_ctx() = 'research_share')
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.research_share_links TO jbrain_app")

    # The keystone: the ONLY grant a share visitor gets on the report corpus. Additive
    # and permissive (OR'd with research_reports_domain), but the domain policy denies an
    # empty-scope non-owner, so for the share context this is the sole path. It matches the
    # pinned link by id, re-checks revocation, and matches either the report link's
    # report_id or the group link's group_id (dynamic membership). The link id is compared
    # as TEXT, never casting the `app.principal_id` GUC to uuid: a SELECT policy is also
    # evaluated on INSERT ... RETURNING rows, and Postgres constant-folds a `::uuid` cast,
    # so a non-uuid principal (e.g. the 'worker' system context) would throw at plan time.
    # Text compare fails closed instead — an empty/non-uuid pin simply matches no link. The
    # subquery reads research_share_links under this same context, admitted by
    # research_share_links_public_read.
    op.execute(
        """
        CREATE POLICY research_reports_share ON app.research_reports FOR SELECT
        USING (
            app.auth_ctx() = 'research_share'
            AND EXISTS (
                SELECT 1 FROM app.research_share_links l
                WHERE l.id::text = current_setting('app.principal_id', true)
                  AND l.revoked_at IS NULL
                  AND (
                    l.report_id = app.research_reports.id
                    OR l.group_id = app.research_reports.group_id
                  )
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY research_reports_share ON app.research_reports")
    op.execute("DROP TABLE app.research_share_links")
