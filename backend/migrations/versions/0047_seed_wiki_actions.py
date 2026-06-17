"""Seed the four wiki-builder schedules/triggers/pipelines (Phase-6 Wave C2a §3b).

The wiki actions (`wiki_refresh` / `wiki_rebuild` / `wiki_reindex` / `wiki_prune`) live in the
in-code registry only (`jbrain.wiki.actions.WIKI_SPECS`, composed into the worker/api registry
at boot like `EVAL_RUN_SPEC` / `PURGE_ACTION` / the reconcilers), so this migration references
them by name and deliberately does NOT touch `app.actions` (its RLS test asserts an exact
six-row set). Each gets a one-action pipeline + a schedule + a `manual=true` trigger so it is
Ops-fireable.

Schedules (UTC, staggered after the 02:00 graph sweeps + 03:00 eval, per §3b). All four are
seeded DISABLED in C2a: the builder ships with the deterministic `StubRewriter` (terse,
non-prose), so auto-running it nightly would populate the live wiki + the landing/search rails
with placeholder articles. The schedules are Ops-fireable on demand (manual triggers) for
testing; a SEPARATE migration (0048, with Wave C2b's LLM rewriter + grounding gate) flips the
nightly `wiki_refresh` (03:30) and `wiki_prune` (03:45) to enabled. `wiki_rebuild`/`wiki_reindex`
stay Ops-manual (disabled schedule).

`next_run_at` is seeded app-side-advanceable; a fixed UUID per trigger makes it addressable by
the Ops/run-log surfaces across environments.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-17
"""

import json

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _next_run_sql(interval: str) -> str:
    """The next UTC occurrence of `date_trunc('day') + interval`, rolling to tomorrow if today's
    moment has passed (so a fresh install never fires immediately)."""
    return (
        f"(date_trunc('day', now() AT TIME ZONE 'UTC') + interval '{interval}'"
        f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
        f"            + interval '{interval}'"
        f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
    )


# (action, pipeline, schedule_id, trigger_id, params, enabled, interval, description)
_SEEDS = [
    (
        "wiki_refresh",
        "nightly_wiki_refresh",
        "00000000-0000-0000-0000-0000000f0001",
        "00000000-0000-0000-0000-0000000f0002",
        {},
        False,
        "3 hours 30 minutes",
        "Rebuild dirty entities' wiki articles (dirty-bit driven).",
    ),
    (
        "wiki_prune",
        "nightly_wiki_prune",
        "00000000-0000-0000-0000-0000000f0003",
        "00000000-0000-0000-0000-0000000f0004",
        {},
        False,
        "3 hours 45 minutes",
        "Archive orphaned wiki articles (after the nightly refresh).",
    ),
    (
        "wiki_rebuild",
        "wiki_rebuild_all",
        "00000000-0000-0000-0000-0000000f0005",
        "00000000-0000-0000-0000-0000000f0006",
        {"target": "all"},
        False,
        "4 hours",
        "Full re-derive of every wiki article (Ops-manual).",
    ),
    (
        "wiki_reindex",
        "wiki_reindex_all",
        "00000000-0000-0000-0000-0000000f0007",
        "00000000-0000-0000-0000-0000000f0008",
        {},
        False,
        "4 hours",
        "Re-embed all wiki section summaries (Ops-manual, after an embed-model swap).",
    ),
]


def upgrade() -> None:
    for action, pipeline, sched_id, trig_id, params, enabled, interval, desc in _SEEDS:
        steps = json.dumps([{"action": action, "action_version": 1, "params": params}])
        op.execute(
            "INSERT INTO app.pipelines (name, version, steps, description)"
            f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(desc)})"
        )
        on = str(enabled).lower()
        op.execute(
            "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
            f" VALUES ('{sched_id}', 86400, 'UTC', {_next_run_sql(interval)}, {on})"
        )
        op.execute(
            "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
            f" VALUES ('{trig_id}', '{sched_id}', {_q(pipeline)}, true)"
        )


def downgrade() -> None:
    for _action, pipeline, sched_id, trig_id, *_rest in _SEEDS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trig_id}'")
        op.execute(f"DELETE FROM app.schedules WHERE id = '{sched_id}'")
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
