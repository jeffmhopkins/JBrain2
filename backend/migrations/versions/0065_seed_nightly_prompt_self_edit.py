"""Seed the nightly `prompt_self_edit` schedule, trigger, and pipeline (Loop 4, Wave 3).

The opt-in `prompt_self_edit` action (the Loop-4 autonomous drafter, in-code only like
`skill_distill` / `correction_mine` / `eval_run`) becomes a data-defined nightly schedule,
**disabled by default** (`enabled=false`) and emergency-fireable from Ops (`manual=true`,
`POST /ops/triggers/{id}/run`) without a restart — mirroring the wiki sweeps (0047/0048).

It reads a durable, owner-origin signal (proposals the owner rejected, bucketed by the
internal job that produced them) and, when a source crosses a threshold, drafts a
`prompt-edit` Proposal against that source's prompt for owner review — propose-only, never
applied (#6). Budget-gated (`SelfImprovementGate`) and kill-switchable; the schedule stays
off until the owner turns it on.

No new tables; no `app.actions` row — `prompt_self_edit` lives in the in-code registry
(`PROMPT_SELF_EDIT_SPEC`), so this references it by name and does NOT touch app.actions
(whose RLS test asserts an exact shipped set). A scheduled trigger has no per-fire payload,
so the (empty) params ride the pipeline step.

Scheduling: nightly (86400s). `next_run_at` seeded to the next 04:00 UTC — an hour after
the 03:00 eval run, after the graph/wiki sweeps — but inert while disabled. A fixed UUID
makes the trigger addressable by the Ops/run-log surfaces across environments.

Revision ID: 0065
Revises: 0064
Create Date: 2026-06-18
"""

import json

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

_ACTION = "prompt_self_edit"
_PIPELINE = "nightly_prompt_self_edit"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000c0019"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000c001a"
_DESCRIPTION = "Draft prompt-edit proposals for prompts whose proposals the owner keeps rejecting."
_PARAMS: dict[str, object] = {}

_RUN_HOUR = 4  # an hour after the 03:00 eval run; inert while the schedule is disabled.
_NEXT_RUN_SQL = (
    f"(date_trunc('day', now() AT TIME ZONE 'UTC')"
    f" + interval '{_RUN_HOUR} hours'"
    f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
    f"            + interval '{_RUN_HOUR} hours'"
    f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
)


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    steps = json.dumps([{"action": _ACTION, "action_version": 1, "params": _PARAMS}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESCRIPTION)})"
    )
    # enabled=false: ships off; the owner turns it on from Ops when they want it.
    op.execute(
        f"""
        INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)
        VALUES ('{_SCHEDULE_ID}', 86400, 'UTC', {_NEXT_RUN_SQL}, false)
        """
    )
    op.execute(
        f"""
        INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)
        VALUES ('{_TRIGGER_ID}', '{_SCHEDULE_ID}', {_q(_PIPELINE)}, true)
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER_ID}'")
    op.execute(f"DELETE FROM app.schedules WHERE id = '{_SCHEDULE_ID}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}' AND version = 1")
