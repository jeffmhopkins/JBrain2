"""Task groups: owner-named buckets tasks are sorted into + an explicit order.

The Tasks surface used to auto-bucket into two fixed system sections
(Scheduled / On demand). `task_groups` lets the owner name their own buckets and
`tasks.group_id` / `tasks.position` carry membership + a persisted order within a
bucket (GUI review Direction B — chips + move sheet, docs/mocks/task-grouping/).
Groups are owner-only metadata (RLS `is_owner()`, like `tasks` itself, 0093).
Deleting a group leaves its tasks intact but ungrouped (ON DELETE SET NULL) — an
ungrouped task simply falls to the trailing "Ungrouped" section on the client.

Revision ID: 0136
Revises: 0135
Create Date: 2026-07-18
"""

from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.task_groups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            name text NOT NULL,
            position int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX task_groups_order_idx ON app.task_groups (principal_id, position)")
    op.execute("ALTER TABLE app.task_groups ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.task_groups FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY task_groups_owner ON app.task_groups
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.task_groups TO jbrain_app")

    # Membership + intra-group order. A NULL group_id is the trailing "Ungrouped"
    # bucket; position is the rank within a task's own group (0-based, dense after a
    # reorder). Deleting the group SET NULLs the task rather than cascading it away.
    op.execute(
        "ALTER TABLE app.tasks"
        " ADD COLUMN group_id uuid REFERENCES app.task_groups(id) ON DELETE SET NULL,"
        " ADD COLUMN position int NOT NULL DEFAULT 0"
    )
    op.execute("CREATE INDEX tasks_group_order_idx ON app.tasks (principal_id, group_id, position)")


def downgrade() -> None:
    op.execute("DROP INDEX app.tasks_group_order_idx")
    op.execute("ALTER TABLE app.tasks DROP COLUMN position, DROP COLUMN group_id")
    op.execute("DROP TABLE app.task_groups")
