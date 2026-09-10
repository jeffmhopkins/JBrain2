"""Which notes the EMR importer OWNS the graph writes for
(docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md W4/D9).

A note whose facts come from the deterministic EMR parse has one writer, and it is
not the model. Two reasons, and both are mechanical rather than stylistic:

- **`fhir_status` is not expressible as a model field** (TOOL_SURFACE gap 3). It is
  EMR-only, set by the parser, and `supersession._lab_status_transition` is what reads
  it — the transition that keeps a FHIR *preliminary* reading from becoming a citable
  current value (plan constraint 4). `assert_fact` has no such field and cannot grow
  one without an `enum`-shaped vocabulary the sidecars may not carry (constraint 8). A
  lab value the model wrote is therefore a lab value the lifecycle cannot supersede.
- **`settle_note` is whole-note** (constraint 6), and the importer is the writer that
  settles this note. That reason used to be filed here as PROSPECTIVE, on the reasoning
  that the conversation's write path is `commit_facts` only
  (`agent/graphwritetools.py`) and calls `settle_note` nowhere. That reasoning was wrong
  in the way that matters: a producer does not need a sweep of its own to LOSE, only a
  co-writer that has one — and `integrate_note` and `emr_parse` both had one, each
  retracting the other's facts on every settle of the note. That is fixed rather than
  avoided: the sweep is scoped by `settle_owners` (`analysis/settle_owner.py`,
  docs/plans/SETTLE_OWNERSHIP.md S1), so each producer releases only its own claim and a
  row survives while any producer still asserts it — a co-writer on one note is no longer
  a data-loss condition. The conversation has a sweep of its own now (S3), and it changes
  nothing here: `narrow_for_emr` leaves it no write verb on such a note, so it stamps no
  claim on one and its release finds nothing to let go of. The `fhir_status` reason
  above is what keeps the narrowing in place regardless, and it is sufficient on its own
  — it is a lifecycle the model-facing verbs cannot express, not a sweep collision.

So the note conversation over an EMR note runs with NO graph-write verb — see
`agents.narrow_for_emr`, which is applied on the unattended pass AND on the owner's
reply turn, and independently in the worker's per-note registry so the two locks fail
separately. The conversation is still opened, still visible, and still holds
`ask_owner` and the entity reads: what it loses is the ability to write a fact the
deterministic parse is authoritative for.

The predicate MIRRORS THE SHIPPED TRIGGER FILTER (migration 0122): a health `Records`
note carrying an EMR archive or a decrypted PDF is exactly the note `emr_import` /
`emr_parse` fire on. Deriving "the importer owns this" from anything else would let the
two disagree — a note the importer writes and the conversation also writes is the state
this module exists to make unreachable.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The `destination` marker the owner picks for an EMR drop (0122's `payload_equals`).
EMR_DESTINATION = "Records"

#: The automatic firewall the EMR triggers pin (0122's `domains`). A `Records` note in
#: any other domain is not an EMR import and keeps the ordinary write surface.
EMR_DOMAIN = "health"

PDF_MEDIA_TYPE = "application/pdf"
ZIP_MEDIA_TYPES = ("application/zip", "application/x-zip-compressed")

#: The media types whose presence marks a note as EMR-importer-owned: the archive
#: stage 1 decrypts, and the PDFs stage 2 parses. Either one means the importer has
#: this note — stage 1's re-ingest turns the first into the second, and the
#: conversation must not hold write verbs in the window between them.
EMR_MEDIA_TYPES = frozenset({PDF_MEDIA_TYPE, *ZIP_MEDIA_TYPES})


def emr_owned(domain: str | None, destination: str | None, media_types: Iterable[str]) -> bool:
    """True when the EMR importer owns this note's graph writes.

    Takes plain values rather than a `NoteInfo` so the EMR package stays importable from
    the agent layer without dragging the notes service in behind it.
    """
    if (domain or "") != EMR_DOMAIN or (destination or "") != EMR_DESTINATION:
        return False
    return any(t in EMR_MEDIA_TYPES for t in media_types)
