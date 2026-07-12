"""JPet talk intent classifier (docs/plans/JPET_V3_PLAN.md W3) — pure, no DB, no LLM.

Proves the keyword router matches common kid requests to an action or a colour (so talk
never needs the LLM to do something), folds colour aliases, and returns None for genuinely
open-ended input (which falls through to the LLM).
"""

from jbrain.jpet.intents import PET_COLORS, Intent, canonical_color, chat_reply, classify
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
        ("play guitar!", "guitar"),
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
        "guitar",
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
    # "original"/"normal" restore the default (null-colour) look
    assert _c("go back to normal").value == "default"
    assert _c("original colour").value == "default"
    assert "default" in PET_COLORS


def test_small_talk_is_not_a_command_and_gets_a_funny_fallback() -> None:
    # Small talk must NOT classify as a command — it flows to the LLM for a real chat.
    for phrase in ("how are you today", "i love you", "tell me a joke", "what are you doing"):
        assert classify(phrase) is None, phrase
    # …with `chat_reply` as the no-LLM fallback: a real emote + a specific funny line.
    for phrase in ("how are you", "i love you", "whats your name"):
        reply = chat_reply(phrase)
        assert reply is not None, phrase
        emote, speech = reply
        assert emote in BUTTON_ACTIONS and len(speech) > 3


def test_word_boundary_stops_command_false_hits() -> None:
    # The classic substring false-positives must not fire commands out of chit-chat.
    assert classify("how are you today") is None  # "day" must not hit `lights`
    assert classify("what is the weather") is None  # "eat" must not hit `eat`
    assert classify("i want a great big hug") is None  # "eat" inside "great"


def test_new_silly_actions_classify() -> None:
    assert _c("do a fart").value == "fart"
    assert _c("do a big burp").value == "burp"
    assert _c("sing a song").value == "sing"
    for a in ("fart", "burp", "sing"):
        assert a in BUTTON_ACTIONS


def test_open_ended_falls_through_to_the_llm() -> None:
    # Genuinely open-ended input (no command, no colour, no small-talk phrase) → None,
    # so the API's LLM + babble pool answer it.
    assert classify("what is the capital of france") is None
    assert classify("") is None
    assert classify("   ") is None


def test_turn_object_recolors_that_object_not_the_robot() -> None:
    # "turn X <colour>" with a named room thing → a recolor intent carrying the target; a bare
    # colour with no target still recolours the robot (the original behaviour).
    for phrase, target, color in [
        ("turn the floor blue", "floor", "blue"),
        ("make the walls green", "walls", "green"),
        ("turn the piano rainbow", "synth", "rainbow"),
        ("turn the drums red please", "drums", "red"),
        ("turn the bed purple", "bed", "purple"),
    ]:
        i = _c(phrase)
        assert i.kind == "recolor" and i.target == target and i.value == color, phrase
    # "turn X normal" resets that object's colour (colour "default"), not the robot's.
    reset = _c("turn the floor normal")
    assert reset.kind == "recolor" and reset.target == "floor" and reset.value == "default"
    # No target → the robot's own colour (unchanged behaviour).
    assert _c("turn red").kind == "color"


def test_make_bigger_smaller_normal_resizes_a_target_or_the_robot() -> None:
    for phrase, target, direction in [
        ("make the bed bigger", "bed", "grow"),
        ("make the piano smaller", "synth", "shrink"),
        ("make the drums huge", "drums", "grow"),
        ("make the ball tiny", "ball", "shrink"),
        ("make me bigger", "robot", "grow"),          # no room thing → the pet grows
        ("make it smaller", "robot", "shrink"),
        ("make the bed normal", "bed", "reset"),      # per-target size reset
        ("make me normal", "robot", "reset"),
    ]:
        i = _c(phrase)
        assert i.kind == "resize" and i.target == target and i.value == direction, phrase


def test_size_words_do_not_fire_on_ordinary_chit_chat() -> None:
    # The strong-only size vocabulary (never bare "big"/"small"/"little") must not turn a hug or
    # a passing "little" into a resize.
    assert classify("i want a great big hug") is None
    assert classify("aww you are such a little cutie") is None


def test_canonical_color_validates() -> None:
    assert canonical_color("Red") == "red"
    assert canonical_color("aqua") == "cyan"
    assert canonical_color("original") == "default"
    assert canonical_color("not-a-colour") is None
