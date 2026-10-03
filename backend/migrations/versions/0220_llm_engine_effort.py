"""`llm_engine_effort` — the reasoning level each task (or tier) runs at ON a given engine.

While Flash-Next serves, every local call is remapped onto it (FLASH_NEXT_ENGINE_PLAN §4c) and
carries the effort its Standard pick would have — chosen for gpt-oss or Grok, not for Flash-Next.
The owner wants Flash-Next's own levels, per task and per tier, WITHOUT touching the Standard
picks, so switching back restores everything exactly. A separate table rather than another key
in `llm_task_overrides` is what makes that true by construction: nothing that writes the
Standard map can reach these rows, and nothing here is read while Standard serves.

`engine` is a column though only `flash-next` is written today: the next engine with a sole
model gets the same control without a migration. `scope` is `task` or `tier`; `key` is the task
name or the tier id. A task row wins over its tier's row, which wins over today's behaviour.

**No principal column, deliberately — the `app.settings` pattern (0012), not `owner_prefs`.**
This is box configuration read on every routed call by the api AND the worker, both under the
fixed system context (`queue.SYSTEM_CTX`, principal `worker`); a row keyed to the owner's real
principal id would be invisible to the router that has to apply it. The firewall is the same as
the settings table's: owner-only (`app.is_owner()`, ENABLE + FORCE), plus 0178's restrictive
deny of jmolt's auth context, since a jmolt-writable knob on how hard the model thinks is a
self-tuning lever for the one session that reads strangers' posts.

DELETE is granted (unlike `app.settings`): clearing a level is the row going away, so the
level falls back to its tier or to today's behaviour instead of pinning a "default" value.

Revision ID: 0220
Revises: 0219
Create Date: 2026-10-03
"""

from alembic import op

revision = "0220"
down_revision = "0219"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.llm_engine_effort (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            engine text NOT NULL,
            scope text NOT NULL CHECK (scope IN ('task', 'tier')),
            key text NOT NULL,
            -- The superset every engine's levels are drawn from; which of them an engine
            -- honors is the catalog's call and is checked by the API, not here.
            effort text NOT NULL CHECK (effort IN ('none', 'low', 'medium', 'high')),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (engine, scope, key)
        )
        """
    )
    op.execute("ALTER TABLE app.llm_engine_effort ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.llm_engine_effort FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY llm_engine_effort_owner ON app.llm_engine_effort
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # RESTRICTIVE so it can only subtract from the owner policy (0178's reasoning).
    op.execute(
        """
        CREATE POLICY llm_engine_effort_not_jmolt ON app.llm_engine_effort
        AS RESTRICTIVE
        USING (app.auth_ctx() IS DISTINCT FROM 'jmolt')
        WITH CHECK (app.auth_ctx() IS DISTINCT FROM 'jmolt')
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.llm_engine_effort TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.llm_engine_effort")
