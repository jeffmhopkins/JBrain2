"""jmolt's scratchpad + its append-only archive (docs/plans/JMOLT_PLAN.md, W2).

jmolt's only cross-night memory: a small set of files it authors and organizes itself
(quota — 16 files / 128 KB / 24 KB each — enforced in the repo write path, not here).
Every change also appends to an out-of-band archive OUTSIDE that budget, so its
consolidations are auditable and any error (or memory-injection) is diffable and
recoverable — the science instrument and the rollback story (§3, M13).

RLS is the M19 split, and it hinges on the fact that jmolt AND jerv both run as the
owner principal, so `is_owner()` cannot tell them apart. Instead:

- **SELECT** is gated on `has_domain_scope('jmolt')`: jmolt's nightly session and jerv's
  narrowed observation session both carry `domain_scopes=('jmolt',)` and may read; a
  session in neither domain (a non-owner, or an owner-scoped session without the jmolt
  scope) sees nothing.
- **WRITE** (INSERT/UPDATE/DELETE on the live table) is pinned to `auth_ctx() = 'jmolt'`
  — set ONLY by jmolt's own nightly launcher. jerv's observation session carries the
  jmolt read scope but NOT that auth context, so it can read and never mutate — a
  Postgres guarantee, not a tool-allowlist promise.

The archive is append-only to jmolt's TOOLS — no scratch tool issues a DELETE or UPDATE,
so jmolt-the-agent can only append. It is never UPDATE-able at all (no UPDATE grant), and
a non-jmolt session (jerv's observation) can neither write nor delete it. The one deletion
path is the repo's bounded retention prune (keep the last N versions per file), which runs
under jmolt's own auth context as part of a write — so the archive stays bounded (M13)
without ever exposing an agent-reachable way to erase recent history.
"""

from alembic import op

revision = "0173"
down_revision = "0172"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jmolt_scratch (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id text NOT NULL,
            filename text NOT NULL,
            content text NOT NULL DEFAULT '',
            bytes int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (principal_id, filename)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.jmolt_scratch_archive (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            filename text NOT NULL,
            content text NOT NULL,
            bytes int NOT NULL,
            op text NOT NULL CHECK (op IN ('write', 'delete')),
            archived_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX jmolt_scratch_archive_idx"
        " ON app.jmolt_scratch_archive (principal_id, filename, archived_at DESC)"
    )

    for table in ("jmolt_scratch", "jmolt_scratch_archive"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        # Read: anyone holding the jmolt domain scope (jmolt itself, jerv's observation
        # session, or the unrestricted owner). A session in neither domain sees nothing.
        op.execute(
            f"""
            CREATE POLICY {table}_read ON app.{table}
            FOR SELECT USING (app.has_domain_scope('jmolt'))
            """
        )

    # Write: the live scratchpad is writable ONLY under jmolt's own auth context, and a
    # new/updated row must carry jmolt's OWN principal id (M19(d) — jmolt writes only its
    # own rows). principal_id is pinned in WITH CHECK (INSERT/UPDATE) only, not USING, so a
    # DELETE of an own row (and system cleanup) still works.
    op.execute(
        """
        CREATE POLICY jmolt_scratch_write ON app.jmolt_scratch
        FOR ALL USING (app.auth_ctx() = 'jmolt')
        WITH CHECK (
            app.auth_ctx() = 'jmolt'
            AND principal_id = current_setting('app.principal_id', true)
        )
        """
    )
    # The archive is append-only to jmolt's TOOLS (no scratch tool issues DELETE/UPDATE);
    # only the repo's bounded retention prune deletes, under jmolt's own auth context, and
    # a non-jmolt session (jerv's observation) can never mutate it. INSERT pins the
    # principal id like the live table; a DELETE policy under jmolt auth lets the prune run.
    op.execute(
        """
        CREATE POLICY jmolt_scratch_archive_append ON app.jmolt_scratch_archive
        FOR INSERT WITH CHECK (
            app.auth_ctx() = 'jmolt'
            AND principal_id = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute(
        """
        CREATE POLICY jmolt_scratch_archive_prune ON app.jmolt_scratch_archive
        FOR DELETE USING (app.auth_ctx() = 'jmolt')
        """
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jmolt_scratch TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON app.jmolt_scratch_archive TO jbrain_app")
    # The archive's bigserial needs sequence USAGE for the INSERT to draw nextval.
    op.execute("GRANT USAGE ON SEQUENCE app.jmolt_scratch_archive_seq_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jmolt_scratch_archive")
    op.execute("DROP TABLE IF EXISTS app.jmolt_scratch")
