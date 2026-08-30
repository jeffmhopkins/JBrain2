"""Storage for the jmolt simulator's recorded platform snapshots.

A corpus (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S1) is one harvest of Moltbook plus jmolt's
scratchpad as of a given night. It lives on the box rather than in the tree because it is
data, not code: it is captured from the live platform, it is large, and it changes whenever a
night worth reproducing happens. Storing it here is also what makes the harness operable by an
owner with no terminal (CLAUDE.md, non-negotiable 10) — harvest, list and run are all debug
API calls against rows, never files someone has to scp.

**Owner-only, and jmolt must never read it.** A corpus contains third-party text harvested
from a hostile-by-assumption platform, and jmolt reading a snapshot of a night would be jmolt
reading a transcript of its own past behaviour re-entering as trusted context. The read policy
therefore requires `is_owner()` AND `auth_ctx() <> 'jmolt'`, matching the split the settings
table uses (migration 0178) rather than the jmolt-domain-scope pattern its own tables use.
"""

from alembic import op

revision = "0181"
down_revision = "0180"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jmolt_sim_corpus (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            note text NOT NULL DEFAULT '',
            captured_at timestamptz NOT NULL DEFAULT now(),
            body jsonb NOT NULL,
            scratch jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX jmolt_sim_corpus_recent ON app.jmolt_sim_corpus (captured_at DESC)")
    op.execute("ALTER TABLE app.jmolt_sim_corpus ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jmolt_sim_corpus FORCE ROW LEVEL SECURITY")
    # One policy per command, all with the same predicate: the owner, and never jmolt.
    for name, cmd in (
        ("read", "SELECT"),
        ("write", "INSERT"),
        ("purge", "DELETE"),
    ):
        clause = "WITH CHECK" if cmd == "INSERT" else "USING"
        op.execute(
            f"""
            CREATE POLICY jmolt_sim_corpus_{name} ON app.jmolt_sim_corpus
            FOR {cmd} {clause} (app.is_owner() AND app.auth_ctx() <> 'jmolt')
            """
        )
    op.execute("GRANT SELECT, INSERT, DELETE ON app.jmolt_sim_corpus TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jmolt_sim_corpus")
