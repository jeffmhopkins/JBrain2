"""The one place a note's text is assembled from its body and its clarifications.

D6 says a note keeps its original body frozen and gains appended, timestamped
clarification blocks — as a STORAGE decision with no bespoke rendering. So the blocks
are plain text appended after the body, and every reader of a note's text calls
`compose_body`: the notes repo (list/get/PATCH, and therefore the note view and
`read_note`), the ingest chunker (D7 — a block must be a chunk of the same note or a
fact extracted from it has nothing to cite), and the integrator's chunkless fallback.

The append is after the body and nowhere else. Facts carry `char_start`/`char_end`
spans into chunks and `_locate` resolves surfaces by offset, so anything inserted
before or inside the body would silently move every existing citation. With no
clarifications `compose_body` returns the body object itself — byte-identical, which is
what keeps every already-anchored offset in the corpus honest.

`strip_clarifications` is the inverse the write path needs: the note editor loads the
note's `body` field (composed) and PATCHes the whole thing back, so without it an
untouched save would bake the blocks into the body column and then double them on the
next read.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from jbrain.models.notes import NoteClarification

# The block's opening marker. Also the seam `strip_clarifications` cuts on, so it has to
# be distinctive enough that owner prose does not contain it by accident — a blank line
# followed by this literal.
_MARK = "[clarification "
_SEAM = "\n\n" + _MARK


def _stamp(at: datetime) -> str:
    # Rows read back from Postgres are tz-aware; a hand-built one in a unit test may not
    # be, and `astimezone` would then read it as LOCAL time and print a wrong instant.
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def clarification_block(c: NoteClarification) -> str:
    """One block as it appears in the note's text. Plain text on purpose: the stream
    renders a note body as text, not markdown, so `**bold**` would show its asterisks."""
    return f"{_MARK}{_stamp(c.created_at)}]\nQ: {c.question}\nA: {c.answer}"


def compose_body(body: str, clarifications: Sequence[NoteClarification]) -> str:
    """The note's full text: the author's body, then each block in `seq` order."""
    if not clarifications:
        return body
    return body + "".join("\n\n" + clarification_block(c) for c in clarifications)


def strip_clarifications(text: str) -> str:
    """Recover the author's body from text that round-tripped through the editor.

    Cut at the FIRST seam rather than matching the exact composed suffix: an owner who
    edited inside the appended region still lands on "everything before the first
    block", instead of writing a mangled copy of the blocks into the body column. Edits
    to a block's own text are discarded, which is the D6 guarantee — a clarification is
    an appended record of what was said, not a field.
    """
    return text.split(_SEAM, 1)[0]
