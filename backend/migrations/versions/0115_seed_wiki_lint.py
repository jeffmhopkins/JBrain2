"""Seed the nightly `wiki_lint` schedule, trigger, and pipeline (Phase-6 follow-on).

The corpus-wide wiki health sweep (docs/plans/WIKI_LINT_PLAN.md) becomes a data-defined nightly
schedule — **disabled by default** (`enabled=false`) and emergency-fireable from Ops
(`manual=true`, `POST /ops/triggers/{id}/run`) without a restart. Mirrors 0066 (hygiene sweeps)
and 0047 (wiki builder sweeps).

In-code only like the four builder actions and the hygiene sweeps: `wiki_lint` lives in the
in-code registry (composed into the worker at boot, `wiki/lint.py`), so this references it by name
and does NOT touch `app.actions` (whose RLS test asserts an exact shipped set). A scheduled trigger
has no per-fire payload (empty params).

Scheduling: nightly (86400s) at 04:00 UTC — after the 03:45 `wiki_prune`, so a lint run audits the
freshly-pruned state. Inert while disabled; the owner enables it from Ops once the deterministic
slice is trusted (the 0047→0048 enable-migration precedent). The fixed UUIDs make the trigger
addressable by the Ops/run-log surfaces across environments.

Revision ID: 0115
Revises: 0114
Create Date: 2026-07-03
"""

import json

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None

_PIPELINE = "nightly_wiki_lint"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000c0021"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000c0022"
_RUN_HOUR = 4  # after wiki_prune's 03:45
_DESC = "Corpus-wide wiki health audit: report drift, re-dirty stale index."


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants)."""
    return "'" + value.replace("'", "''") + "'"


def _next_run_sql(run_hour: int) -> str:
    return (
        f"(date_trunc('day', now() AT TIME ZONE 'UTC')"
        f" + interval '{run_hour} hours'"
        f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
        f"            + interval '{run_hour} hours'"
        f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
    )


def upgrade() -> None:
    steps = json.dumps([{"action": "wiki_lint", "action_version": 1, "params": {}}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESC)})"
    )
    # enabled=false: ships off; the owner turns it on from Ops when they want it.
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_SCHEDULE_ID}', 86400, 'UTC', {_next_run_sql(_RUN_HOUR)}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_TRIGGER_ID}', '{_SCHEDULE_ID}', {_q(_PIPELINE)}, true)"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER_ID}'")
    op.execute(f"DELETE FROM app.schedules WHERE id = '{_SCHEDULE_ID}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}' AND version = 1")
