"""Unit tests for span-keyed resolution pins (plan N10).

Pure: occurrence-indexing, re-location across edits, and the build/validity
guards that keep a re-run convergent without silent flips.
"""

from jbrain.analysis.pins import (
    build_pin,
    locate_occurrence,
    occurrence_index_at,
    pin_holds,
    span_text_hash,
)


def test_occurrence_index_at_first_and_second():
    text = "Globex memo. Later, Globex again."
    first = text.index("Globex")
    second = text.index("Globex", first + 1)
    assert occurrence_index_at(text, "Globex", first) == 0
    assert occurrence_index_at(text, "Globex", second) == 1


def test_occurrence_index_at_wrong_offset_is_none():
    text = "Globex"
    assert occurrence_index_at(text, "Globex", 1) is None  # not at this offset


def test_occurrence_index_at_empty_surface_is_none():
    assert occurrence_index_at("anything", "", 0) is None


def test_locate_occurrence_finds_nth():
    text = "a X b X c X"
    assert locate_occurrence(text, "X", 0) == text.index("X")
    assert locate_occurrence(text, "X", 2) == text.rindex("X")


def test_locate_occurrence_missing_is_none():
    assert locate_occurrence("a b c", "X", 0) is None
    assert locate_occurrence("X only once", "X", 1) is None  # no 2nd occurrence


def test_build_pin_rejects_empty_surface():
    assert (
        build_pin(
            note_id="n",
            chunk_id="c",
            decision_kind="identity",
            text="hi",
            surface="",
            start=0,
            entity_id="e1",
        )
        is None
    )


def test_build_pin_rejects_surface_not_at_offset():
    # A fabricated/paraphrased span that isn't actually at `start` can't be pinned.
    assert (
        build_pin(
            note_id="n",
            chunk_id="c",
            decision_kind="identity",
            text="my wife Celine",
            surface="Celine",
            start=0,
            entity_id="e1",
        )
        is None
    )


def test_build_pin_captures_occurrence_index():
    text = "Bob and later Bob"
    second = text.index("Bob", 1)
    pin = build_pin(
        note_id="n",
        chunk_id="c",
        decision_kind="identity",
        text=text,
        surface="Bob",
        start=second,
        entity_id="e2",
    )
    assert pin is not None
    assert pin.occurrence_index == 1
    assert pin.entity_id == "e2"
    assert pin.span_text_hash == span_text_hash("Bob")


def test_pin_holds_when_text_unchanged():
    text = "I work at Globex now."
    pin = build_pin(
        note_id="n",
        chunk_id="c",
        decision_kind="identity",
        text=text,
        surface="Globex",
        start=text.index("Globex"),
        entity_id="e1",
    )
    assert pin is not None
    assert pin_holds(pin, text) is True


def test_pin_holds_across_insert_above_shifting_offsets():
    # The A8 case: a paragraph inserted ABOVE shifts char offsets but the
    # occurrence of "Globex" is unchanged, so the pin must still hold.
    text = "I work at Globex now."
    pin = build_pin(
        note_id="n",
        chunk_id="c",
        decision_kind="identity",
        text=text,
        surface="Globex",
        start=text.index("Globex"),
        entity_id="e1",
    )
    assert pin is not None
    edited = "A new first paragraph was added.\n\n" + text
    assert pin_holds(pin, edited) is True


def test_pin_invalidated_when_surface_removed():
    text = "I work at Globex now."
    pin = build_pin(
        note_id="n",
        chunk_id="c",
        decision_kind="identity",
        text=text,
        surface="Globex",
        start=text.index("Globex"),
        entity_id="e1",
    )
    assert pin is not None
    assert pin_holds(pin, "I changed jobs entirely.") is False


def test_pin_for_second_occurrence_invalid_if_one_removed():
    # Pinned the 2nd "Bob"; an edit leaves only one "Bob" → no 2nd occurrence → re-decide.
    text = "Bob and later Bob"
    pin = build_pin(
        note_id="n",
        chunk_id="c",
        decision_kind="identity",
        text=text,
        surface="Bob",
        start=text.index("Bob", 1),
        entity_id="e2",
    )
    assert pin is not None and pin.occurrence_index == 1
    assert pin_holds(pin, "Bob, just once now.") is False


def test_predicate_key_pin():
    text = "weighs 182 lb"
    pin = build_pin(
        note_id="n",
        chunk_id="c",
        decision_kind="predicate_key",
        text=text,
        surface="weighs",
        start=text.index("weighs"),
        normalized_predicate="weight",
    )
    assert pin is not None
    assert pin.decision_kind == "predicate_key"
    assert pin.normalized_predicate == "weight"
    assert pin_holds(pin, text) is True
