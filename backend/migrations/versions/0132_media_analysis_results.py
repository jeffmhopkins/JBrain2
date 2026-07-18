"""Run-scoped results for deferred media-analysis tool calls (DEFERRED_TOOL_CALLS_PLAN.md P2).

A URL video analysis (`analyze_stream` in `full` / long `window` mode) is too slow to
run inside a chat turn, so it defers to a background job: the turn ends, a `task_status`
card shows progress, and the finished analysis auto-resumes into the chat. Unlike the
attachment path — whose result caches on `attachment_extracts` keyed by the blob's
sha256 — a URL has no attachment to hang a cache on, so the deferred result needs its
own home. This is that home: one row per deferred analysis, holding its live progress,
final result (in the `video_analysis` view's data shape so the card swaps straight to
the existing component), and status, scoped to the chat session that kicked it.

Owner-only, exactly like `app.jobs` / `app.runs`: `jerv` runs as the owner principal and
the row carries chat output (sampled frames, a summary, a transcript), never a domain
fact, so there is no `domain_code` firewall column — the RLS test proves a scoped
non-owner principal can neither read nor write it. The row reaps with its `run_id`
(ON DELETE CASCADE), so the existing runs reaper (0129) cleans it up; a null `run_id`
(a result written without an audit run) is left to the session-scoped TTL.

Revision ID: 0132
Revises: 0131
Create Date: 2026-07-18
"""

from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.media_analysis_results (
            id uuid PRIMARY KEY,
            -- the agent chat session that kicked this analysis: the card is scoped to
            -- it and the finished result auto-resumes into it.
            session_id text NOT NULL,
            -- the audit run tracking the background job, if one was opened; the row
            -- reaps with it (a purged run reaps its result). Nullable: progress + result
            -- live on THIS row, so an audit run is optional.
            run_id uuid REFERENCES app.runs(id) ON DELETE CASCADE,
            -- the queue job doing the work (to cancel it); SET NULL if the job reaps first.
            job_id uuid REFERENCES app.jobs(id) ON DELETE SET NULL,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'done', 'failed', 'canceled')),
            -- structured live progress the task_status card renders: {step, total, label}.
            progress jsonb NOT NULL DEFAULT '{}'::jsonb,
            -- the finished analysis in the video_analysis view's data shape (title, mode,
            -- is_live, youtube_id, stream_url, summary, duration_ms, frames[], transcript),
            -- so the card swaps to the existing component. Null until done.
            result jsonb,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX media_analysis_results_session_idx"
        " ON app.media_analysis_results (session_id, created_at DESC)"
    )

    op.execute("ALTER TABLE app.media_analysis_results ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.media_analysis_results FORCE ROW LEVEL SECURITY")
    # Owner-only, like app.jobs / app.runs: chat output, not domain content — no per-domain
    # firewall. A scoped non-owner principal can neither read nor write (RLS test).
    op.execute(
        "CREATE POLICY media_analysis_results_owner ON app.media_analysis_results"
        " USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.media_analysis_results TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.media_analysis_results")
