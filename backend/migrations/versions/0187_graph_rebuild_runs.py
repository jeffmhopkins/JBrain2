"""The entity-graph rebuild sweep's run state (jbrain.analysis.rebuild).

One row per corpus rebuild: its phase, its resume cursor, and the counters the Ops run
log reports progress from. Without a durable row the sweep is not resumable — a lost
self-enqueue or a worker restart would restart the purge from the beginning and
re-derive notes it had already rebuilt.

RLS: the owner/system posture (`app.is_owner()`, the `triggers`/`schedules`/`agent_runs`
precedent from 0036/0016). The row is sweep metadata — counters and a note id — with no
note content and no domain of its own, so a domain-firewalled policy would have nothing
honest to key on; every owner session (narrowed or not) may read its own maintenance
state, and no capability-token session sees it at all. Isolation test:
tests/integration/test_graph_rebuild_rls.py.

Grants are SELECT/INSERT/UPDATE — no DELETE. A finished run is an audit row like
`eval_runs`: the sweep supersedes it by opening a new one, never by erasing history.

The partial unique index makes two concurrent rebuilds structurally impossible: at most
one row may sit in a non-completed status, so a second "Run now" click continues the
open run instead of opening a rival that would re-purge from a stale cursor.

Revision ID: 0187
Revises: 0186
Create Date: 2026-09-08
"""

from alembic import op

revision = "0187"
down_revision = "0186"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.graph_rebuild_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            status text NOT NULL DEFAULT 'purging'
                CHECK (status IN ('purging', 'draining', 'completed')),
            -- The resume point: the last note rebuilt, ordered by id. NOT created_at,
            -- which is the client's capture time and can be backdated by an offline
            -- flush — a created_at cursor would silently skip such a note.
            cursor_note_id uuid,
            total_notes int NOT NULL DEFAULT 0,
            notes_done int NOT NULL DEFAULT 0,
            facts_purged int NOT NULL DEFAULT 0,
            facts_kept int NOT NULL DEFAULT 0,
            -- The chained full wiki rebuild (0045/0046 leave articles and citations
            -- dangling after a graph re-derive), so the chain is auditable.
            wiki_rebuild_job_id uuid,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX graph_rebuild_runs_one_active"
        " ON app.graph_rebuild_runs ((status <> 'completed'))"
        " WHERE status <> 'completed'"
    )
    op.execute(
        "CREATE INDEX graph_rebuild_runs_started_idx ON app.graph_rebuild_runs (started_at DESC)"
    )
    op.execute("ALTER TABLE app.graph_rebuild_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.graph_rebuild_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY graph_rebuild_runs_owner ON app.graph_rebuild_runs
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.graph_rebuild_runs TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.graph_rebuild_runs")
