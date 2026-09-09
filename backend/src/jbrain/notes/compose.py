"""The one place a note's text is assembled from its body and its clarifications.

D6 says a note keeps its original body frozen and gains appended, timestamped
clarification blocks — as a STORAGE decision with no bespoke rendering. So the blocks
are plain text appended after the body, and every reader of a note's text calls
`compose_body`: the notes repo (list/get/PATCH, and therefore the note view and
`read_note`), the search leg's `body_preview`, the ingest chunker (D7 — a block must be
a chunk of the same note or a fact extracted from it has nothing to cite), and the
integrator's chunkless fallback.

The append is after the body and nowhere else, so composing a note can never move a
character of what was already there. That is a narrower guarantee than it sounds: it is
the CHUNK offsets (`app.chunks.char_start`/`char_end`, indices into this composed text)
that appending cannot shift, and through them the chunk-relative spans on
`app.entity_mentions` that `analysis.pipeline._locate` computes. `app.facts` carries no
span at all — it cites a chunk by id, and what actually threatens that id is the
re-ingest, not the composition (see `ingest.pipeline._carry_over_chunks`). With no
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

_MARK = "[clarification "


def _stamp(at: datetime) -> str:
    # Rows read back from Postgres are tz-aware; a hand-built one in a unit test may not
    # be, and `astimezone` would then read it as LOCAL time and print a wrong instant.
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def clarification_block(c: NoteClarification) -> str:
    """One block as it appears in the note's text.

    Plain text on purpose, and it has to read correctly on BOTH surfaces, which do not
    agree: the stream bubble renders the body as a text child (`Stream.tsx`), so markup
    would show its own asterisks, while the note screen runs it through the assistant's
    Markdown renderer (`NoteScreen.tsx` → `agent/markdown.tsx`), which turns the blank
    line into a paragraph and the two soft newlines into `<br>`s. Plain text is the only
    format that is right in both — and it is where the owner READS a note that the block
    structure actually renders.
    """
    return f"{_MARK}{_stamp(c.created_at)}]\nQ: {c.question}\nA: {c.answer}"


def composed_suffix(clarifications: Sequence[NoteClarification]) -> str:
    """Everything `compose_body` appends after the body — "" when there is nothing."""
    return "".join("\n\n" + clarification_block(c) for c in clarifications)


def compose_body(body: str, clarifications: Sequence[NoteClarification]) -> str:
    """The note's full text: the author's body, then each block in `seq` order."""
    if not clarifications:
        return body
    return body + composed_suffix(clarifications)


def strip_clarifications(text: str, clarifications: Sequence[NoteClarification]) -> str | None:
    """Recover the author's body from text that round-tripped through the editor, or
    `None` when `text` is not this note's composed text with the blocks still intact.

    The suffix is RECONSTRUCTED from the stored rows and removed exactly. The obvious
    cheaper reading — cut at the first `"\\n\\n[clarification "` — is a data-loss bug,
    not a shortcut: that literal is ordinary prose (the owner pastes a clarified note's
    displayed text into a new note, or simply types it), and cutting on it silently and
    permanently deletes every paragraph after it from a body nobody edited. A body must
    never be truncatable by its own content, so nothing here scans `text` for a marker.

    Refusing (`None`) rather than salvaging a prefix is the same rule seen from the
    write side: the only edit this path can apply safely is one that left the appended
    region byte-identical, and any other input is a caller the API should tell, not a
    body the repo should guess at. `update_note` turns it into a 409 with the note
    unchanged; the owner's text is still in the editor, and a body-only edit re-sent
    over an intact suffix saves normally.

    Trailing whitespace is tolerated on both sides because the editor sends
    `body.trim()` (`EditLayer.tsx`), which would otherwise reject a save whose last
    answer happened to end in a newline.
    """
    if not clarifications:
        return text
    suffix = composed_suffix(clarifications).rstrip()
    trimmed = text.rstrip()
    if not trimmed.endswith(suffix):
        return None
    return trimmed[: len(trimmed) - len(suffix)]
