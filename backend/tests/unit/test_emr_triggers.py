"""The seeded EMR-import triggers + worker registration
(docs/plans/EMR_IMPORT_PLAN.md §6.0). Proves the two-stage `payload_equals` gating
on the widened `note.ingested` payload picks the right stage (and only EMR notes),
and that both handlers register with the action registry (the boot bijection).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from jbrain.ingest.emr.import_handler import EMR_PARSE_SPEC
from jbrain.ingest.emr.intake_handler import EMR_IMPORT_SPEC
from jbrain.workflow.contracts import TriggerFilter
from jbrain.workflow.dispatcher import event_matches
from jbrain.workflow.registry import build_registry

_MIG = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0122_seed_emr_import_triggers.py"
)


def _load_migration():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("mig_0122", _MIG)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_migration()
_MARKERS = {action: markers for action, _p, _id, markers, _d in _MOD._EMR_TRIGGERS}


def _filter(action: str) -> TriggerFilter:
    # Built exactly as the migration seeds it, so the test gates on the shipped filter.
    return TriggerFilter(
        event_types=[_MOD._EVENT], domains=["health"], payload_equals=_MARKERS[action]
    )


_ARCHIVE = {  # the raw import note: an encrypted archive, not yet decrypted
    "note_id": "n",
    "destination": "Records",
    "has_zip_attachment": True,
    "has_pdf_attachment": False,
}
_DECRYPTED = {  # after intake: the zip is gone, the PDFs are attached
    "note_id": "n",
    "destination": "Records",
    "has_zip_attachment": False,
    "has_pdf_attachment": True,
}
_NORMAL = {  # an ordinary note — no destination, no attachments
    "note_id": "n",
    "destination": None,
    "has_zip_attachment": False,
    "has_pdf_attachment": False,
}


def test_import_trigger_matches_only_the_archive_stage() -> None:
    f = _filter("emr_import")
    assert event_matches(f, "note.ingested", _ARCHIVE)
    assert not event_matches(f, "note.ingested", _DECRYPTED)  # zip already decrypted
    assert not event_matches(f, "note.ingested", _NORMAL)  # not a Records import note


def test_parse_trigger_matches_only_the_decrypted_stage() -> None:
    f = _filter("emr_parse")
    assert event_matches(f, "note.ingested", _DECRYPTED)
    assert not event_matches(f, "note.ingested", _ARCHIVE)  # still encrypted -> stage 1's job
    assert not event_matches(f, "note.ingested", _NORMAL)


def test_a_records_note_with_no_attachment_trips_neither_stage() -> None:
    # A plain Medical/Records note (no zip, no PDF) flows through ordinary ingestion.
    plain = {
        "note_id": "n",
        "destination": "Records",
        "has_zip_attachment": False,
        "has_pdf_attachment": False,
    }
    assert not event_matches(_filter("emr_import"), "note.ingested", plain)
    assert not event_matches(_filter("emr_parse"), "note.ingested", plain)


def test_triggers_ignore_a_non_ingested_event() -> None:
    # The markers only ride note.ingested; note.created (no attachments yet) is inert.
    assert not event_matches(_filter("emr_import"), "note.created", _ARCHIVE)


async def _noop(_payload: dict) -> None: ...


def test_both_handlers_register_with_the_action_registry() -> None:
    # The boot bijection: each in-code action must bind to a handler and vice versa.
    registry = build_registry((EMR_IMPORT_SPEC, EMR_PARSE_SPEC))
    registry.validate({"emr_import": _noop, "emr_parse": _noop})
    assert EMR_IMPORT_SPEC.handler == "emr_import"
    assert EMR_PARSE_SPEC.handler == "emr_parse"
    assert EMR_IMPORT_SPEC.dedup_key_expr == "note_id"
    assert EMR_PARSE_SPEC.dedup_key_expr == "note_id"
