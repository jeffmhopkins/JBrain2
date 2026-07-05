"""Fast, LLM-free intent classifier for the pet's talk box (docs/archive/JPET_V3_PLAN.md W3).

The hybrid talk→action router: this keyword/rule classifier runs FIRST on a child's
message so simple requests ("dance!", "chase the ball", "turn red") always produce a
visible action with no LLM in the loop. Only genuinely open-ended input falls through to
the LLM — and even that degrades to a friendly wiggle if the LLM is unavailable — so
talking to the pet never silently does nothing (the v2 failure the pivot fixes).

Pure and deterministic: it unit-tests with no DB and no model.
"""

from dataclasses import dataclass

# The named colours the wall knows how to recolour to (mirrored in pet.html). `rainbow`
# cycles. Aliases fold onto these.
PET_COLORS = (
    "cyan",
    "magenta",
    "gold",
    "orange",
    "blue",
    "red",
    "green",
    "pink",
    "purple",
    "white",
    "rainbow",
)
_COLOR_ALIASES = {
    "aqua": "cyan",
    "teal": "cyan",
    "turquoise": "cyan",
    "yellow": "gold",
    "violet": "purple",
    "lilac": "purple",
    "rosy": "pink",
    "crimson": "red",
    "scarlet": "red",
    "lime": "green",
    "rainbowy": "rainbow",
    "colourful": "rainbow",
    "colorful": "rainbow",
}

# Ordered phrase → canned button action (the actions `service.CANNED_SCRIPTS` knows).
# First match wins, so put the more specific phrases first.
_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("jump rope", "skip rope", "skipping"), "jumprope"),
    (("play music", "play a song", "sing", "piano", "synth", "music"), "music"),
    (("chase the ball", "chase", "fetch", "kick the ball", "get the ball", "ball"), "chase"),
    (("hide and seek", "peekaboo", "peek a boo", "go hide", "hide"), "hide"),
    (("dance", "boogie", "dancing"), "dance"),
    (("spin", "twirl", "circles", "circle", "run around"), "spin"),
    (("jump", "hop", "bounce", "leap"), "jump"),
    (("wave", "say hi", "say hello", "hello", "hiya", "hi there"), "wave"),
    (("wiggle", "shake", "giggle"), "wiggle"),
    (("silly", "beep", "boop", "funny sound", "noise", "sound"), "beep"),
    (("come here", "come back", "come to me", "over here"), "come"),
    (("wake up", "wake", "good morning", "get up"), "wake"),
    (("go to sleep", "go to bed", "sleep", "nap", "bedtime", "night night", "tired"), "sleep"),
    (("snack", "eat", "hungry", "food", "dinner", "lunch"), "eat"),
    (("lights", "light switch", "turn off the light", "dark", "day", "night time"), "lights"),
)

# A tiny friendly line to say alongside a matched action (kept generic + safe).
_REPLIES = {
    "dance": "Wheee, watch me dance!",
    "spin": "Round and round!",
    "jump": "Boing boing!",
    "wave": "Hiii!",
    "wiggle": "Wigglewigglewiggle!",
    "chase": "Ball ball ball!",
    "hide": "You can't see me!",
    "beep": "Bee-boo-beep!",
    "come": "Coming!",
    "sleep": "Yawwwn… nighty-night.",
    "wake": "Good morning!",
    "eat": "Yum yum yum!",
    "lights": "Flicky flick!",
    "jumprope": "Jumpy jump rope!",
    "music": "La la laaa!",
}


@dataclass(frozen=True)
class Intent:
    """A classified request. `kind` is 'action' (a canned button action) or 'color'."""

    kind: str
    value: str
    speech: str


def _color_in(words: list[str]) -> str | None:
    for w in words:
        if w in PET_COLORS:
            return w
        if w in _COLOR_ALIASES:
            return _COLOR_ALIASES[w]
    return None


def classify(text: str) -> Intent | None:
    """Match a child's message to a known action or colour, or None when it's open-ended
    (let the LLM handle it). Colour words win ("make it blue"); then the keyword table."""
    t = " ".join(text.lower().split())
    if not t:
        return None
    color = _color_in(t.replace("!", " ").replace(".", " ").split())
    if color is not None:
        say = "Rainbow time!" if color == "rainbow" else f"Ooh, {color}!"
        return Intent(kind="color", value=color, speech=say)
    for phrases, action in _KEYWORDS:
        if any(p in t for p in phrases):
            return Intent(kind="action", value=action, speech=_REPLIES.get(action, "Okay!"))
    return None


def canonical_color(name: str) -> str | None:
    """Fold a colour name/alias onto a known `PET_COLORS` value, or None if unknown."""
    n = name.strip().lower()
    if n in PET_COLORS:
        return n
    return _COLOR_ALIASES.get(n)
