"""Seed the EMR-import event triggers + pipelines (docs/plans/EMR_IMPORT_PLAN.md §6.0).

The importer is a two-stage pipeline driven off `note.ingested`, whose payload now
carries the pre-decryption markers `destination` + `has_zip_attachment` +
`has_pdf_attachment` (ingest/pipeline.py, §12.2 #4). Each stage is a one-action
pipeline bound by a `payload_equals` filter — a precise, user-chosen marker, never
a body-text guess, and health-only (Medical ⇒ `health`, the automatic firewall):

- stage 1 `emr_import`: a health `Records` note that carries an ARCHIVE
  (`has_zip_attachment` true) -> decrypt in place, attach the PDFs, scrub the
  password, and re-ingest. The re-ingest re-emits `note.ingested` with the zip gone.
- stage 2 `emr_parse`: a health `Records` note that carries a decrypted PDF and NO
  archive (`has_zip_attachment` false, `has_pdf_attachment` true) -> parse the PDFs
  into cited facts. No loop: stage 1 can't re-fire (the zip is gone), and stage 2 is
  projection-idempotent.

The `emr_import`/`emr_parse` actions are in-code only (worker.py build_registry, like
the wiki/hygiene sweeps), so nothing seeds `app.actions` here — only the pipelines +
triggers. `pipelines`/`triggers` ship in 0036; fixed UUIDs keep the trigger ids
stable for the run-log / Ops surfaces.

Revision ID: 0122
Revises: 0121
Create Date: 2026-07-04
"""

import json

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

# (action, pipeline name, trigger id, payload_equals markers, description)
_EMR_TRIGGERS = (
    (
        "emr_import",
        "event_emr_import",
        "00000000-0000-0000-0000-0000000e6d01",
        {"destination": "Records", "has_zip_attachment": True},
        "Decrypt a health Records note's EMR archive in place (EMR import, stage 1).",
    ),
    (
        "emr_parse",
        "event_emr_parse",
        "00000000-0000-0000-0000-0000000e6d02",
        {"destination": "Records", "has_zip_attachment": False, "has_pdf_attachment": True},
        "Parse a health Records note's decrypted EMR PDFs into facts (EMR import, stage 2).",
    ),
)

_EVENT = "note.ingested"


def _q(value: str) -> str:
    """A single-quoted SQL string literal (the seed values are trusted module
    constants; this only guards an apostrophe in a description)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for action, pipeline, trigger_id, markers, description in _EMR_TRIGGERS:
        steps = json.dumps([{"action": action, "action_version": 1, "params": {}}])
        op.execute(
            "INSERT INTO app.pipelines (name, version, steps, description)"
            f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(description)})"
        )
        # Event-bound (on_event set). The filter pins the event type, the health domain
        # (the automatic firewall), and the pre-decryption payload markers via
        # payload_equals — extra keys are inert for other note.ingested triggers.
        filter_ = json.dumps(
            {"event_types": [_EVENT], "domains": ["health"], "payload_equals": markers}
        )
        op.execute(
            "INSERT INTO app.triggers (id, on_event, pipeline, filter)"
            f" VALUES ({_q(trigger_id)}, {_q(_EVENT)}, {_q(pipeline)},"
            f" cast({_q(filter_)} AS jsonb))"
        )


def downgrade() -> None:
    for _action, pipeline, trigger_id, _markers, _description in _EMR_TRIGGERS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trigger_id}'")
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
