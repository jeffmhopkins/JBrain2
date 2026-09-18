"""The rebuild sweep's non-DB surface (analysis/rebuild.py).

The behaviour lives against real Postgres (tests/integration/test_graph_rebuild_pg.py);
what is worth pinning here is the contract the worker and the Ops surfaces read: the
action spec the seeded pipelines reference by name, and the progress line the run log
renders.
"""

from jbrain.analysis.rebuild import (
    GRAPH_REBUILD_KIND,
    GRAPH_REBUILD_SPEC,
    IDLE,
    RebuildProgress,
)


def test_spec_handler_matches_the_job_kind_the_sweep_self_enqueues() -> None:
    # The sweep continues itself by enqueuing its own handler key; a drift here would
    # queue a job no handler can claim and strand every rebuild after its first batch.
    assert GRAPH_REBUILD_SPEC.handler == GRAPH_REBUILD_KIND
    assert GRAPH_REBUILD_SPEC.name == "graph_rebuild"


def test_spec_is_mutating_and_domain_optional() -> None:
    # Ops renders blast radius from these: it purges every note's derived graph, and it
    # runs corpus-wide under SYSTEM_CTX rather than inside one domain.
    assert GRAPH_REBUILD_SPEC.mutating
    assert GRAPH_REBUILD_SPEC.domain_optional


def test_idle_progress_reports_no_run() -> None:
    assert IDLE.run_id is None
    assert IDLE.processed_now == 0
    assert IDLE.note == "no graph rebuild in progress"


def test_progress_note_reports_kept_facts_alongside_the_count() -> None:
    # Pinned survivors are the point of the whole sweep, so the operator sees them.
    progress = RebuildProgress(
        run_id="r", status="purging", total=500, done=120, purged=3400, kept=12
    )
    assert progress.note == (
        "purging: 120/500 notes rebuilt, 3400 facts re-derived, 12 pinned facts kept"
    )
