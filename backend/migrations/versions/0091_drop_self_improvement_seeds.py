"""Tear down the six self-improvement nightly seeds Wave 1 deregistered.

Wave 1 removed the eval/promotion harness and the Loop 2-4 self-improvement actions
(`eval_run`, `skill_distill`, `skill_sweep`, `predicate_review`, `correction_mine`,
`prompt_self_edit`) from the in-code action registry. Their seeded schedule/trigger/
pipeline rows (migrations 0044/0054/0055/0058/0059/0065) survived, leaving triggers
that point at actions the registry no longer resolves. The five Loop rows ship
disabled (inert), but `nightly_eval_run` (0044) was seeded ENABLED — so on the first
due night the scheduler tick would resolve its pipeline and raise an uncaught
`ActionRegistryError` mid-tick. This migration deletes all six seeds so no schedule
references a missing action.

Delete order is FK-safe: triggers reference schedules (`on_schedule_id`) and pipelines
(by name), so triggers go first, then schedules, then pipelines. None of the source
seeds inserted pipeline_steps or any rows beyond the pipeline/schedule/trigger triple,
so there is nothing else to remove.

`downgrade()` re-inserts each seed verbatim from its source migration's `upgrade()`
(same ids, params, manual flags, descriptions, `enabled` defaults, and `next_run_at`
expressions) for exact reversibility to the prior DB state.

Revision ID: 0091
Revises: 0090
Create Date: 2026-06-24
"""

import json

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None

# (pipeline name, schedule id, trigger id) for each deregistered seed.
_EVAL_RUN = (
    "nightly_eval_run",
    "00000000-0000-0000-0000-0000000c0017",
    "00000000-0000-0000-0000-0000000c0018",
)
_SKILL_DISTILL = (
    "nightly_skill_distill",
    "00000000-0000-0000-0000-0000000f0009",
    "00000000-0000-0000-0000-0000000f000a",
)
_SKILL_SWEEP = (
    "nightly_skill_sweep",
    "00000000-0000-0000-0000-0000000f000b",
    "00000000-0000-0000-0000-0000000f000c",
)
_PREDICATE_REVIEW = (
    "nightly_predicate_review",
    "00000000-0000-0000-0000-0000000f000d",
    "00000000-0000-0000-0000-0000000f000e",
)
_CORRECTION_MINE = (
    "nightly_correction_mine",
    "00000000-0000-0000-0000-0000000f000f",
    "00000000-0000-0000-0000-0000000f0010",
)
_PROMPT_SELF_EDIT = (
    "nightly_prompt_self_edit",
    "00000000-0000-0000-0000-0000000c0019",
    "00000000-0000-0000-0000-0000000c001a",
)

_SEEDS = (
    _EVAL_RUN,
    _SKILL_DISTILL,
    _SKILL_SWEEP,
    _PREDICATE_REVIEW,
    _CORRECTION_MINE,
    _PROMPT_SELF_EDIT,
)


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants; guards an
    apostrophe in a description)."""
    return "'" + value.replace("'", "''") + "'"


def _hour_next_run_sql(hours: int) -> str:
    """The seed `next_run_at` expression: today's `hours:00` UTC, rolled to tomorrow
    if that instant has already passed. Mirrors 0044/0065 verbatim."""
    return (
        f"(date_trunc('day', now() AT TIME ZONE 'UTC')"
        f" + interval '{hours} hours'"
        f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
        f"            + interval '{hours} hours'"
        f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
    )


def _interval_next_run_sql(interval: str) -> str:
    """The seed `next_run_at` expression used by the Loop 2-3 seeds (0054/0055/0058/
    0059), which express the run offset as an interval string. Mirrors them verbatim."""
    return (
        f"(date_trunc('day', now() AT TIME ZONE 'UTC') + interval '{interval}'"
        f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
        f"            + interval '{interval}'"
        f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
    )


def upgrade() -> None:
    # FK-safe order across all six: triggers -> schedules -> pipelines.
    for _pipeline, _schedule_id, trigger_id in _SEEDS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trigger_id}'")
    for _pipeline, schedule_id, _trigger_id in _SEEDS:
        op.execute(f"DELETE FROM app.schedules WHERE id = '{schedule_id}'")
    for pipeline, _schedule_id, _trigger_id in _SEEDS:
        op.execute(f"DELETE FROM app.pipelines WHERE name = {_q(pipeline)} AND version = 1")


def _seed_pipeline(pipeline: str, action: str, params: dict, description: str) -> None:
    steps = json.dumps([{"action": action, "action_version": 1, "params": params}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(description)})"
    )


def downgrade() -> None:
    # Re-insert each seed exactly as its source migration's upgrade() did.

    # 0044 — nightly_eval_run (seeded ENABLED via the schedules.enabled default).
    _seed_pipeline(
        _EVAL_RUN[0],
        "eval_run",
        {"suite": "all", "version_label": "nightly"},
        "Score the note.extract eval suite against the live model (budget-gated).",
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)"
        f" VALUES ('{_EVAL_RUN[1]}', 86400, 'UTC', {_hour_next_run_sql(3)})"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_EVAL_RUN[2]}', '{_EVAL_RUN[1]}', '{_EVAL_RUN[0]}', true)"
    )

    # 0054 — nightly_skill_distill (DISABLED).
    _seed_pipeline(
        _SKILL_DISTILL[0],
        "skill_distill",
        {},
        "Distill skills from successful agent runs into owner-reviewed shadow skills.",
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_SKILL_DISTILL[1]}', 86400, 'UTC', {_interval_next_run_sql('4 hours')}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_SKILL_DISTILL[2]}', '{_SKILL_DISTILL[1]}', {_q(_SKILL_DISTILL[0])}, true)"
    )

    # 0055 — nightly_skill_sweep (DISABLED).
    _seed_pipeline(
        _SKILL_SWEEP[0],
        "skill_sweep",
        {},
        "Cap active skills per domain, demoting the least-useful to shadow (reversible).",
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_SKILL_SWEEP[1]}', 86400, 'UTC', {_interval_next_run_sql('5 hours')}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_SKILL_SWEEP[2]}', '{_SKILL_SWEEP[1]}', {_q(_SKILL_SWEEP[0])}, true)"
    )

    # 0058 — nightly_predicate_review (DISABLED).
    _seed_pipeline(
        _PREDICATE_REVIEW[0],
        "predicate_review",
        {},
        "Propose owner-reviewed resolutions for open new_predicate cards.",
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_PREDICATE_REVIEW[1]}', 86400, 'UTC',"
        f" {_interval_next_run_sql('6 hours')}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_PREDICATE_REVIEW[2]}', '{_PREDICATE_REVIEW[1]}',"
        f" {_q(_PREDICATE_REVIEW[0])}, true)"
    )

    # 0059 — nightly_correction_mine (DISABLED).
    _seed_pipeline(
        _CORRECTION_MINE[0],
        "correction_mine",
        {},
        "Mine ended chats for owner corrections; stage owner-reviewed correction notes.",
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_CORRECTION_MINE[1]}', 86400, 'UTC',"
        f" {_interval_next_run_sql('7 hours')}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_CORRECTION_MINE[2]}', '{_CORRECTION_MINE[1]}',"
        f" {_q(_CORRECTION_MINE[0])}, true)"
    )

    # 0065 — nightly_prompt_self_edit (DISABLED).
    _seed_pipeline(
        _PROMPT_SELF_EDIT[0],
        "prompt_self_edit",
        {},
        "Draft prompt-edit proposals for prompts whose proposals the owner keeps rejecting.",
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_PROMPT_SELF_EDIT[1]}', 86400, 'UTC', {_hour_next_run_sql(4)}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_PROMPT_SELF_EDIT[2]}', '{_PROMPT_SELF_EDIT[1]}',"
        f" {_q(_PROMPT_SELF_EDIT[0])}, true)"
    )
