"""`endpoint_status` — the last thing each panel said about itself, kept.

**This reverses a deliberate decision, and the reason it was made still stands.** The telemetry
route said, in as many words: *"Nothing is stored. These are a panel's own claims about itself,
they are only ever read by a human looking at a log, and a table would be a schema to migrate
every time the question changes."* The objection was schema churn, and it was right — the report
has gained `screen`, `heard`, `tap`, `restart_why` and `panel_reset` since, and each one would
have been a migration.

What was wrong was the premise that a human reads the log. **The owner has no terminal**
(CLAUDE.md #10). The only way to answer "is her panel alive, and did the update land" was to
pull the box's log through the debug API and read it — which is what "just your update only has
0.2.88" cost, on a panel that had in fact updated forty minutes earlier. A question the owner
asks from a phone cannot be answered by a log only a shell can reach.

**So: one row per panel, and the report itself as `jsonb`.** That keeps the original objection
answered — a field added to the report needs no migration, because there are no columns to add —
while making the answer reachable from the PWA. `version` and `reported_at` are lifted out
because they are what every query sorts and filters on, and a panel that has never reported has
no row at all, which is itself the answer to "has this one ever been alive".

It is a SNAPSHOT, not a history. One row, upserted. A history table is a different feature with
a different retention question, and the `pmu_history` ring inside the report already carries the
only series anything has needed.

**A panel may write only its own row, and the policy says so rather than the route.** These are
unauthenticated-to-each-other devices on children's walls; a panel that could overwrite its
sibling's status could report that twin as dead, or as running a version it is not, and the
owner's only view of the fleet would be lying to him. `0178_settings_deny_jmolt` is the argument
for why "the handler writes `principal.id`" is a code-review convention rather than a mechanism.

Revision ID: 0210
Revises: 0209
Create Date: 2026-09-23
"""

from alembic import op

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.endpoint_status (
            principal_id uuid PRIMARY KEY REFERENCES app.principals(id) ON DELETE CASCADE,
            reported_at timestamptz NOT NULL DEFAULT now(),
            version text NOT NULL DEFAULT '',
            report jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )

    op.execute("ALTER TABLE app.endpoint_status ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.endpoint_status FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY endpoint_status_owner ON app.endpoint_status
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # ITS OWN ROW, BOTH WAYS. `USING` decides which rows it may see and update — which is what
    # makes the upsert's conflict resolution find its own row and nobody else's — and
    # `WITH CHECK` decides which rows it may leave behind, so it cannot write a row under
    # another principal's id even by asking for one.
    op.execute(
        """
        CREATE POLICY endpoint_status_own ON app.endpoint_status
        USING (
            current_setting('app.principal_kind', true) = 'device_key'
            AND principal_id::text = current_setting('app.principal_id', true)
        )
        WITH CHECK (
            current_setting('app.principal_kind', true) = 'device_key'
            AND principal_id::text = current_setting('app.principal_id', true)
        )
        """
    )
    # RLS decides WHICH rows; the grant decides whether the app role may reach the table at
    # all. Without it every query is `permission denied` before a policy is consulted.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.endpoint_status TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.endpoint_status")
