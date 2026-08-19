"""What the box was DOING to the GPU, so the vitals graph can be read.

The vitals detail surface plots GPU busy % beside a roster of agent turns, and the two
regularly disagree: the trace sits pinned at 94% with an empty roster, because the
heaviest work this box does is not a turn. Loading gpt-oss-120b reads tens of GB into
unified memory; the eviction that made room for it freed another model; an image render
takes the whole pool. None of that had a record, so the screen could only say "the GPU
is busy with something else" and leave the owner to guess which something.

`app.box_events` is that record: one row per notable box event, opened when the work
starts (`ended_at` NULL while it runs, which is what lets the screen say "loading
oss120b…" rather than only reporting it afterwards) and closed when it finishes.

A TABLE rather than the in-memory ring the GPU samples use (jbrain.vitals_ring) because
this one has to be cross-process: the api and the worker each load models through their
own gateway, so a process-local ring would show whichever half happened to run in the
process serving the page. Volume is tiny — a handful of rows an hour, pruned at a day by
the worker — so a plain btree-indexed table is right; no hypertable.

Owner-only RLS (host telemetry is not domain data), matching app.host_metrics.

Revision ID: 0167
Revises: 0166
Create Date: 2026-08-19
"""

from alembic import op

revision = "0167"
down_revision = "0166"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.box_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            kind text NOT NULL,
            subject text NOT NULL DEFAULT '',
            detail text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'ok',
            source text NOT NULL DEFAULT ''
        )
        """
    )
    # The only read is "the trailing N seconds, newest first" — the window the graph is
    # showing — so one descending index on the clock column answers every query.
    op.execute("CREATE INDEX box_events_at_idx ON app.box_events (at DESC)")
    op.execute("ALTER TABLE app.box_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.box_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY box_events_owner ON app.box_events
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # The api and the worker both open/close rows; the worker also prunes.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.box_events TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.box_events")
