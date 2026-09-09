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
    composed = compose_body(BODY, [block("Which 10k?", "The canal loop.", 3)])
    assert strip_clarifications(composed) == BODY


def test_strip_leaves_an_unclarified_body_alone() -> None:
    assert strip_clarifications(BODY) == BODY


def test_strip_cuts_at_the_first_block_even_when_the_rest_was_edited() -> None:
    """An owner who typed inside the appended region still lands on their own body,
    rather than writing a mangled copy of the blocks into the body column."""
    composed = compose_body(
        BODY, [block("Which 10k?", "The canal loop.", 3), block("Alone?", "With Dana.", 47)]
    )
    mauled = composed.replace("With Dana.", "with dana i think")
    assert strip_clarifications(mauled) == BODY
