"""Report folders: owner-named buckets the Research Library sorts reports into.

The Research Library's Reports tab used to be a flat list. `report_groups` lets the
owner name their own folders and `research_reports.group_id` carries a report's
membership (a default/"Ungrouped" bucket + custom folders), mirroring the Tasks
surface's `task_groups` (0137). The folders are OWNER-ONLY metadata (RLS
`is_owner()`), deliberately kept off the corpus firewall: a report lives in the
`external` domain (0140) so jerv can search it, but its FOLDER is a private browse
convenience jerv never reads. `group_id` is therefore an opaque uuid on the corpus
row — set only under the full-owner context, never selected by jerv's corpus tools.

Deleting a folder leaves its reports intact but unfiled (ON DELETE SET NULL) — an
unfiled report simply falls to the trailing "Ungrouped" section on the client, the
same shape as a deleted task group.

Revision ID: 0149
Revises: 0148
Create Date: 2026-07-27
"""

from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.report_groups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            name text NOT NULL,
            position int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX report_groups_order_idx ON app.report_groups (principal_id, position)")
    op.execute("ALTER TABLE app.report_groups ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.report_groups FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY report_groups_owner ON app.report_groups
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.report_groups TO jbrain_app")

    # A report's folder. NULL = the trailing "Ungrouped" section. Deleting the folder
    # SET NULLs the report rather than deleting it (a folder is a browse convenience;
    # losing it must never lose the report). Opaque to jerv — the corpus read/search
    # tools select explicit columns, never group_id, so foldering leaks nothing to the
    # external scope.
    op.execute(
        "ALTER TABLE app.research_reports"
        " ADD COLUMN group_id uuid REFERENCES app.report_groups(id) ON DELETE SET NULL"
    )
    op.execute("CREATE INDEX research_reports_group_idx ON app.research_reports (group_id)")


def downgrade() -> None:
    op.execute("DROP INDEX app.research_reports_group_idx")
    op.execute("ALTER TABLE app.research_reports DROP COLUMN group_id")
    op.execute("DROP TABLE app.report_groups")
