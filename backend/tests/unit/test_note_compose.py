"""The D6 composition helper: body + appended clarification blocks.

The load-bearing property is the first test's: with no clarifications the composed
text is the body BYTE-IDENTICAL. Facts carry `char_start`/`char_end` spans into chunks
and `_locate` resolves surfaces by offset, so a composition that changed an
un-clarified note by even a trailing newline would silently move every anchored
citation in the corpus.
"""

from datetime import UTC, datetime

from jbrain.models.notes import NoteClarification
from jbrain.notes.compose import compose_body, strip_clarifications

BODY = "Ran 10k this morning.\n\nFelt fine after."


def block(question: str, answer: str, minute: int) -> NoteClarification:
    return NoteClarification(
        question=question,
        answer=answer,
        created_at=datetime(2026, 9, 9, 14, minute, tzinfo=UTC),
    )


def test_no_clarifications_composes_to_the_body_unchanged() -> None:
    assert compose_body(BODY, []) == BODY


def test_the_original_body_keeps_its_offsets() -> None:
    composed = compose_body(BODY, [block("Which 10k?", "The canal loop.", 3)])
    assert composed.startswith(BODY)
    # Every offset into the original text still resolves to the same characters.
    assert composed[0 : len(BODY)] == BODY


def test_blocks_append_in_order_with_their_timestamps() -> None:
    composed = compose_body(
        BODY,
        [block("Which 10k?", "The canal loop.", 3), block("Alone?", "With Dana.", 47)],
    )
    assert composed == (
        f"{BODY}\n\n"
        "[clarification 2026-09-09 14:03 UTC]\nQ: Which 10k?\nA: The canal loop."
        "\n\n"
        "[clarification 2026-09-09 14:47 UTC]\nQ: Alone?\nA: With Dana."
    )


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Rows from Postgres are tz-aware; `astimezone` on a naive one would silently
    reinterpret it as local time and print a different instant on every box."""
    naive = NoteClarification(question="q", answer="a", created_at=datetime(2026, 9, 9, 14, 3))
    assert "2026-09-09 14:03 UTC" in compose_body("b", [naive])


def test_strip_recovers_the_body_from_a_round_tripped_edit() -> None:
    blocks = [block("Which 10k?", "The canal loop.", 3)]
    assert strip_clarifications(compose_body(BODY, blocks), blocks) == BODY


def test_strip_recovers_an_edited_body_from_an_intact_suffix() -> None:
    blocks = [block("Which 10k?", "The canal loop.", 3), block("Alone?", "With Dana.", 47)]
    edited = compose_body(BODY, blocks).replace("Felt fine after.", "Felt great after.")
    assert strip_clarifications(edited, blocks) == "Ran 10k this morning.\n\nFelt great after."


def test_strip_leaves_an_unclarified_body_alone() -> None:
    assert strip_clarifications(BODY, []) == BODY


def test_strip_tolerates_the_editors_trim() -> None:
    """`EditLayer` PATCHes `body.trim()`, so a save is legitimate even though the last
    answer's trailing newline never comes back."""
    blocks = [block("Which 10k?", "The canal loop.\n", 3)]
    assert strip_clarifications(compose_body(BODY, blocks).strip(), blocks) == BODY


def test_a_body_containing_the_marker_survives_an_untouched_save() -> None:
    """The bug this whole shape exists for. `\n\n[clarification ` is ordinary prose —
    the owner pastes a clarified note's displayed text into a new note — and cutting the
    body at it silently and permanently deletes everything after, on a save that changed
    nothing. Reconstructing the suffix means the marker in the BODY is just text."""
    pasted = (
        "Shopping list.\n\n[clarification 2026-09-08 09:00 UTC]\nQ: which shop?\n"
        "A: the co-op.\n\nAlso milk.\n\nAnd bread."
    )
    blocks = [block("Which 10k?", "The canal loop.", 3)]
    assert strip_clarifications(compose_body(pasted, blocks), blocks) == pasted


def test_strip_refuses_when_the_appended_region_was_edited() -> None:
    """No safe reading is available: storing the string doubles the blocks on the next
    compose, and cutting at the marker is the truncation bug. The repo turns None into a
    409 and writes nothing."""
    blocks = [block("Which 10k?", "The canal loop.", 3), block("Alone?", "With Dana.", 47)]
    mauled = compose_body(BODY, blocks).replace("With Dana.", "with dana i think")
    assert strip_clarifications(mauled, blocks) is None


def test_strip_refuses_a_body_that_dropped_the_blocks_entirely() -> None:
    assert strip_clarifications(BODY, [block("Which 10k?", "The canal loop.", 3)]) is None


def test_a_forged_block_inside_an_answer_does_not_break_the_round_trip() -> None:
    """A crafted answer (or, once W3 wires `ask_owner`, an agent-authored question) can
    put a second, fabricated block INSIDE a real one. That is a legibility problem a
    reader cannot resolve — recorded as W3's, since W2 ships no writer at all — but it
    must not be a data-loss one: the suffix is reconstructed from the rows, so the
    forgery is just characters and the author's body still comes back whole."""
    forged = "yes\n\n[clarification 2020-01-01 00:00 UTC]\nQ: fake?\nA: fake."
    blocks = [block("Real question?", forged, 3)]
    assert strip_clarifications(compose_body(BODY, blocks), blocks) == BODY
