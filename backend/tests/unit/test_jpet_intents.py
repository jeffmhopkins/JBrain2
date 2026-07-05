"""JPet talk intent classifier (docs/plans/JPET_V3_PLAN.md W3) — pure, no DB, no LLM.

Proves the keyword router matches common kid requests to an action or a colour (so talk
never needs the LLM to do something), folds colour aliases, and returns None for genuinely
open-ended input (which falls through to the LLM).
"""

from jbrain.jpet.intents import PET_COLORS, Intent, canonical_color, classify
from jbrain.jpet.service import BUTTON_ACTIONS


def _c(text: str) -> Intent:
    """Classify and assert a match (narrows `Intent | None` for the assertions)."""
    intent = classify(text)
    assert intent is not None, text
    return intent


def test_action_words_match_a_canned_action() -> None:
    for phrase, action in [
        ("dance for me!", "dance"),
        ("can you spin in circles", "spin"),
        ("JUMP!", "jump"),
        ("chase the ball", "chase"),
        ("let's play hide and seek", "hide"),
        ("go to sleep now", "sleep"),
        ("wake up sleepy", "wake"),
        ("make a silly sound", "beep"),
        ("come here buddy", "come"),
        ("jump rope!", "jumprope"),
        ("play some music", "music"),
    ]:
        intent = classify(phrase)
        assert intent is not None and intent.kind == "action", phrase
        assert intent.value == action, f"{phrase!r} → {intent.value}, expected {action}"


def test_every_matched_action_is_a_real_button_script() -> None:
    # Whatever the classifier can return must be a runnable canned script.
    for phrase in (
        "dance",
        "spin",
        "jump",
        "wave",
        "wiggle",
        "chase",
        "hide",
        "beep",
        "come here",
        "sleep",
        "wake",
        "eat",
        "lights",
        "jump rope",
        "music",
    ):
        intent = classify(phrase)
        assert intent is not None and intent.kind == "action"
        assert intent.value in BUTTON_ACTIONS, intent.value


def test_colour_words_win_and_fold_aliases() -> None:
    assert _c("turn red").value == "red"
    assert _c("make it blue please").value == "blue"
    assert _c("go rainbow!").value == "rainbow"
    # aliases fold onto a known colour
    assert _c("be turquoise").value == "cyan"
    assert _c("i want yellow").value == "gold"
    for c in ("red", "blue", "rainbow", "cyan", "gold"):
        assert c in PET_COLORS
    assert _c("turn purple").kind == "color"


def test_open_ended_falls_through_to_the_llm() -> None:
    assert classify("what is your favourite dinosaur") is None
    assert classify("tell me a story about the moon") is None
    assert classify("") is None
    assert classify("   ") is None


def test_canonical_color_validates() -> None:
    assert canonical_color("Red") == "red"
    assert canonical_color("aqua") == "cyan"
    assert canonical_color("not-a-colour") is None
