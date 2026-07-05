"""Fast, LLM-free intent classifier for the pet's talk box (docs/archive/JPET_V3_PLAN.md W3).

The hybrid talk→action router: this keyword/rule classifier runs FIRST on a child's
message so simple requests ("dance!", "chase the ball", "turn red") always produce a
visible action with no LLM in the loop, and common small talk ("how are you", "tell me a
joke", "I love you") gets a funny canned reply + emote. Only genuinely open-ended input
falls through to the LLM — and even that degrades to a random funny babble (CHAT_BABBLE)
if the LLM is unavailable — so talking to the pet always holds a silly little conversation.

Pure and deterministic: it unit-tests with no DB and no model.
"""

import re
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
    "default",  # the original synthwave look (no override) — the wall renders null-colour
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
    "original": "default",
    "normal": "default",
    "reset": "default",
    "robot": "default",
}

# Ordered phrase → canned button action (the actions `service.CANNED_SCRIPTS` knows).
# First match wins, so put the more specific phrases first.
_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("jump rope", "skip rope", "skipping"), "jumprope"),
    (("play guitar", "guitar", "strum"), "guitar"),
    (("play music", "play a song", "piano", "synth", "music"), "music"),
    (("sing", "sing a song", "la la", "sing me"), "sing"),
    (("chase the ball", "chase", "fetch", "kick the ball", "get the ball", "ball"), "chase"),
    (("hide and seek", "peekaboo", "peek a boo", "go hide", "hide"), "hide"),
    (("dance", "boogie", "dancing"), "dance"),
    (("spin", "twirl", "circles", "circle", "run around"), "spin"),
    (("jump", "hop", "bounce", "leap"), "jump"),
    (("wave", "say hi", "say hello", "hiya", "hi there"), "wave"),
    (("wiggle", "shake", "giggle"), "wiggle"),
    (("fart", "toot", "poot", "poof", "stinky"), "fart"),
    (("burp", "belch", "burpy"), "burp"),
    (("silly", "beep", "boop", "funny sound", "noise"), "beep"),
    (("come here", "come back", "come to me", "over here"), "come"),
    (("wake up", "wake", "good morning", "get up"), "wake"),
    (("go to sleep", "go to bed", "sleep", "nap", "bedtime", "night night", "tired"), "sleep"),
    (("snack", "eat", "hungry", "food", "dinner", "lunch"), "eat"),
    (("light switch", "turn off the light", "turn on the light", "lights"), "lights"),
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
    "guitar": "Strum strum strum!",
    "sing": "🎵 La la laaa, I love youuu! 🎵",
    "fart": "Pfffffbt! Hee-hee, 'scuse me!",
    "burp": "BUUURP! Hehe, that was a big one!",
}

# Conversation — small talk a 3–4-year-old actually says, matched to a funny, kid-safe reply
# and a little emote, so the pet holds a silly chat with NO LLM in the loop. `(phrases, emote,
# reply)`; the emote must be a real button action. Checked after the command keywords.
_CHAT: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("how are you", "how do you feel", "are you ok", "are you okay"),
        "wiggle",
        "Sooo happy and wiggly, thank you! How are YOU?",
    ),
    (
        ("i love you", "love you", "love oo", "luv you"),
        "dance",
        "I love oo too! Squishy robot hug! Mwah!",
    ),
    (
        ("tell me a joke", "know a joke", "a joke", "knock knock", "something funny"),
        "beep",
        "Why did the robot go to the party? To boogie his bolts off! Hee-hee-hee!",
    ),
    (
        ("what are you doing", "whatcha doing", "what you doing"),
        "spin",
        "Playing playing playing! Wanna play with me?",
    ),
    (
        ("who are you", "what are you", "are you a robot", "your name", "whats your name"),
        "wave",
        "I'm your silly robot buddy! Beep-boop-beep!",
    ),
    (("thank you", "thanks", "thank oo"), "nod", "You're welcome, best friend!"),
    (
        ("good boy", "good girl", "good robot", "good job", "well done", "good pet"),
        "wiggle",
        "Yaaay! I'm a happy happy bot!",
    ),
    (("are you happy", "you happy"), "jump", "Super-duper-mega happy! Boing!"),
    (
        ("i am sad", "im sad", "are you sad", "feel sad", "feeling sad"),
        "dance",
        "No sad allowed — let's dance the sad away! Wheee!",
    ),
    (("favorite", "favourite", "favrite"), "wiggle", "My favourite thing is YOU! And boops."),
    (
        ("talk to me", "say something", "can you talk", "can you speak"),
        "beep",
        "Boop-bee-doo! I speak fluent robot! Beeble beeble!",
    ),
    (
        ("goodbye", "bye bye", "bye-bye", "see you", "see ya"),
        "wave",
        "Bye bye! Come play again super soon!",
    ),
    (("happy birthday", "its my birthday", "my birthday"), "dance", "Yaaay, party wiggle dance!"),
    (("goodnight", "good night"), "sleep", "Nighty-night! Sweet robot dreams."),
)

# When it's *truly* open-ended and there's no LLM to answer, the pet still babbles back —
# a varied pool of funny, safe lines (the API picks one at random so it never repeats).
CHAT_BABBLE = (
    "Hee-hee! Boop boop!",
    "Beeble-beeble-boop! That tickles my circuits!",
    "Ooh ooh, tell me more! Boop!",
    "Dah-boo! You're my favourite!",
    "Wiggle wiggle — I'm listening!",
    "Bzzt-bloop! Robots love chatting!",
    "Hee-hee, you're silly! I like silly!",
    "Beep? Beep beep! (that means yay!)",
    "Mee love oo! Boop on the nose!",
    "La-la-boop! Wanna play a game?",
)


@dataclass(frozen=True)
class Intent:
    """A classified request. `kind` is 'action' (a canned button action), 'chat' (a small
    emote + a funny conversational reply), or 'color'. 'chat' and 'action' both run `value`'s
    canned script; only the speech differs."""

    kind: str
    value: str
    speech: str


def _match(t: str, phrases: tuple[str, ...]) -> bool:
    """True if any phrase occurs in `t` on word boundaries — so "day" doesn't fire on
    "to*day*" and "eat" doesn't fire on "w*eat*her" (raw substring matching's false hits)."""
    return any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", t) for p in phrases)


def _color_in(words: list[str]) -> str | None:
    for w in words:
        if w in PET_COLORS:
            return w
        if w in _COLOR_ALIASES:
            return _COLOR_ALIASES[w]
    return None


def classify(text: str) -> Intent | None:
    """Match a child's message to a COMMAND only — a colour ("make it blue") or a play action
    ("dance", "chase the ball", "do a fart"). Returns None for everything else — greetings,
    questions, chit-chat — so it flows to the LLM for a *real* conversation (with `chat_reply`
    as the no-LLM fallback). Commands act instantly; talk actually talks back."""
    t = " ".join(text.lower().split())
    if not t:
        return None
    color = _color_in(t.replace("!", " ").replace(".", " ").split())
    if color is not None:
        return Intent(kind="color", value=color, speech=color_speech(color))
    for phrases, action in _KEYWORDS:
        if _match(t, phrases):
            return Intent(kind="action", value=action, speech=_REPLIES.get(action, "Okay!"))
    return None


def chat_reply(text: str) -> tuple[str, str] | None:
    """The no-LLM conversation fallback: if the child's message matches a known bit of small
    talk, return `(emote_action, funny_reply)`; else None (the caller picks a random babble).
    Only used when the LLM is unavailable — a real model gives a real back-and-forth."""
    t = " ".join(text.lower().split())
    for phrases, emote, reply in _CHAT:
        if _match(t, phrases):
            return emote, reply
    return None


def color_speech(color: str) -> str:
    """A friendly line for a colour change (shared by the classifier and the palette action)."""
    if color == "rainbow":
        return "Rainbow time!"
    if color == "default":
        return "Back to my old self!"
    return f"Ooh, {color}!"


def canonical_color(name: str) -> str | None:
    """Fold a colour name/alias onto a known `PET_COLORS` value, or None if unknown."""
    n = name.strip().lower()
    if n in PET_COLORS:
        return n
    return _COLOR_ALIASES.get(n)
