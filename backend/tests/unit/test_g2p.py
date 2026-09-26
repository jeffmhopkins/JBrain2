"""The box's grapheme-to-phoneme converter, and its agreement with what is in flash.

The panel matches PHONEMES, not words, so a wrong conversion here does not raise anything: it
registers a pronunciation nobody in the house says and the wake phrase quietly stops working.
That failure looks exactly like a bad microphone, which is why these tests pin the OUTPUT
STRINGS rather than asserting something was returned.
"""

from __future__ import annotations

import re
from pathlib import Path

from jbrain.g2p import phonemes_for

_VOCAB_C = Path(__file__).resolve().parents[3] / "firmware" / "main" / "vocab.c"
# `{"phrase", "phonemes", ...}` — the table's first two fields; entries carrying NULL (the wake
# phrase, whose phonemes cannot exist until the name does) do not match and are skipped.
_ENTRY = re.compile(r'^\s*\{"([^"]+)",\s*"([^"]*)"', re.MULTILINE)


def test_known_word_converts() -> None:
    # `fish` is the name in use, so this is the conversion that matters today.
    assert phonemes_for("fish") == "Fgs"


def test_words_are_space_separated() -> None:
    # The separator is not cosmetic: MultiNet's language model is over phoneme classes and a
    # space is one of them, so running two words together is a different phrase.
    assert phonemes_for("hey fish") == "hd Fgs"


def test_unknown_word_returns_none() -> None:
    # An INVENTED NAME is the normal case for this branch — CMUdict is a dictionary of words.
    # None is the contract that keeps the panel on its own converter instead of registering a
    # guess, so this is the behaviour a future richer G2P has to preserve or deliberately change.
    assert phonemes_for("elora") is None


def test_one_unknown_word_discards_the_whole_phrase() -> None:
    # ALL OR NOTHING. Half a phrase converted is worse than none of it: the panel would be
    # listening for a pronunciation of the first word and a guess at the second.
    assert phonemes_for("hey elora") is None


def test_blank_input_returns_none() -> None:
    assert phonemes_for("") is None
    assert phonemes_for("   ") is None


def test_case_is_not_significant() -> None:
    # The pet name arrives from a text box in the PWA, so it arrives however it was typed.
    assert phonemes_for("Fish") == phonemes_for("fish")


def test_agrees_with_every_phrase_already_in_flash() -> None:
    """The converter and the firmware's committed table must not drift apart.

    Both sides of this comparison came from Espressif's alphabet, so it is a CONSISTENCY check
    rather than independent verification — worth being plain about. What it catches is the
    thing that actually happens: someone corrects a phoneme in `vocab.c` by hand, or edits the
    transcribed map here, and the wake phrase ends up encoded in a scheme the other 47 phrases
    are not. That would leave the one phrase this module exists to fix as the only one wrong.
    """
    entries = _ENTRY.findall(_VOCAB_C.read_text())
    assert len(entries) > 40, "vocab.c parse looks wrong, not a shrunken table"

    checked = 0
    for phrase, flashed in entries:
        ours = phonemes_for(phrase)
        if ours is None:
            continue  # a phrase with a word CMUdict lacks; the firmware's stands
        assert ours == flashed, f"{phrase!r}: box says {ours!r}, flash carries {flashed!r}"
        checked += 1
    # Guards the loop itself: an `ours is None` for everything would pass silently.
    assert checked > 30, f"only {checked} phrases cross-checked"
