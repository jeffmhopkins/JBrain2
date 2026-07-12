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

# Room things a "turn X <colour>" / "make X bigger" command can point at, plus the robot
# itself. Ordered phrase → canonical target key the WALL renders under (mirrored in pet.html).
# Multi-word phrases come first so "ball pit"/"toy box" win over a bare "ball"/"toy". These are
# EPHEMERAL wall effects (never persisted): a reload resets them. `robot` maps colour to the
# pet's own recolour path and size to the pet's scale.
TARGETS: tuple[tuple[str, str], ...] = (
    ("ball pit", "ball_pit"),
    ("ballpit", "ball_pit"),
    ("toy box", "toy_box"),
    ("toybox", "toy_box"),
    ("keyboard", "synth"),
    ("piano", "synth"),
    ("synth", "synth"),
    ("floor", "floor"),
    ("ground", "floor"),
    ("walls", "walls"),
    ("wall", "walls"),
    ("bed", "bed"),
    ("blocks", "blocks"),
    ("block", "blocks"),
    ("bricks", "blocks"),
    ("brick", "blocks"),
    ("drums", "drums"),
    ("drum", "drums"),
    ("guitar", "guitar"),
    ("ball", "ball"),
    # The robot's recolour zones — recolour only (the wall paints each separately). Before the
    # whole-"robot" target so "turn your head blue" hits the head, not the whole pet. Plurals
    # first so "arms" wins over a stray "arm".
    ("head", "head"),
    ("arms", "arm"),
    ("arm", "arm"),
    ("legs", "leg"),
    ("leg", "leg"),
    ("ears", "ear"),
    ("ear", "ear"),
    ("eyes", "eye"),
    ("eye", "eye"),
    ("mouth", "mouth"),
    ("lips", "mouth"),
    ("body", "body"),
    ("tummy", "body"),
    ("robot", "robot"),
    ("yourself", "robot"),
    ("pet", "robot"),
)
# A friendly spoken name per target for the pet's little reaction line.
_TARGET_NAMES: dict[str, str] = {
    "floor": "floor",
    "walls": "walls",
    "bed": "bed",
    "blocks": "blocks",
    "synth": "piano",
    "drums": "drums",
    "guitar": "guitar",
    "toy_box": "toy box",
    "ball_pit": "ball pit",
    "ball": "ball",
    "head": "head",
    "body": "body",
    "arm": "arms",
    "leg": "legs",
    "ear": "ears",
    "eye": "eyes",
    "mouth": "mouth",
    "robot": "me",
}

# Size words. Kept STRONG and unambiguous (never bare "big"/"small"/"little", which appear in
# ordinary chit-chat like "a great big hug") so a resize only fires on a clear request. `grow`/
# `shrink` step the size up/down; `huge`/`tiny` jump straight to the max/min so "make it HUGE"
# actually reads huge; the reset words restore the normal size.
_HUGE = ("huge", "giant", "gigantic", "enormous", "massive")
_TINY = ("tiny", "teeny", "teensy", "mini", "itty")
_GROW = ("bigger", "biggest", "grow")
_SHRINK = ("smaller", "smallest", "shrink", "littler")
_SIZE_RESET = ("normal", "regular", "reset")

# The shapes the pet can morph into (an EPHEMERAL wall effect — a reload restores the robot).
# "robot" is the default/reset form. Aliases fold onto the canonical creature the wall draws.
FORMS: dict[str, str] = {
    "robot": "robot",
    "dog": "dog",
    "puppy": "dog",
    "doggy": "dog",
    "doggie": "dog",
    "cat": "cat",
    "kitty": "cat",
    "kitten": "cat",
    "kitticat": "cat",
    "dragon": "dragon",
    "dino": "dragon",
    "dinosaur": "dragon",
    "cow": "cow",
    "moo": "cow",
    "pig": "pig",
    "piggy": "pig",
    "piglet": "pig",
    "chicken": "chicken",
    "chick": "chicken",
    "hen": "chicken",
    "birdie": "chicken",
}
# A form command needs a transformation verb so "do you like cats?" never morphs the pet — a
# creature word alone isn't enough; it must be paired with one of these.
_FORM_TRIGGERS = (
    "change",
    "become",
    "transform",
    "morph",
    "turn into",
    "turn in to",
    "turn to",
    "be a",
    "be an",
    "be my",
    "you're a",
    "youre a",
    "you are a",
    "make you",
    "make yourself",
    "turn you",
    "shift into",
    "into a",
    "into an",
)

# "reset everything" — one command that clears EVERY ephemeral effect (colours, sizes, form)
# and restores the pet's own colour too. Checked first, since "reset" alone folds to a colour.
_RESET_ALL = (
    "reset everything",
    "reset it all",
    "reset all",
    "reset the room",
    "reset the whole",
    "clear everything",
    "clear it all",
    "everything back to normal",
    "all back to normal",
    "put everything back",
    "undo everything",
    "undo it all",
    "start over",
    "back to normal everything",
)

# "Clean up your room" — a tidy chore. Paired with a bedtime phrase ("and go to bed") it chains
# into sleep (tidy → TV + lights off → bed). Checked ahead of the plain keyword table so the
# compound form wins over the bare "sleep"/"bed" keyword.
_CLEANUP = (
    "clean up",
    "cleanup",
    "clean your room",
    "clean my room",
    "clean the room",
    "clean up your room",
    "tidy up",
    "tidy your room",
    "tidy my room",
    "tidy the room",
    "pick up your toys",
    "pick up the blocks",
    "put your toys away",
    "put the toys away",
)
_BEDTIME = (
    "go to bed",
    "goto bed",
    "go to sleep",
    "and sleep",
    "then sleep",
    "bedtime",
    "night night",
    "nighty night",
    "time for bed",
    "off to bed",
)

# Ordered phrase → canned button action (the actions `service.CANNED_SCRIPTS` knows).
# First match wins, so put the more specific phrases first.
_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    # Creature tricks first (they're specific). The wall only draws the flame / egg when the pet
    # is that creature, so "breathe fire" as a robot is a harmless little emote.
    (("breathe fire", "breath fire", "blow fire", "fire breath", "flames", "fire"), "fire"),
    (("lay an egg", "lay a egg", "lay egg", "laying", "egg"), "lay"),
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
    "fire": "RAAAWR! 🔥 Fire breath!",
    "lay": "Bok bok bok… plop! I laid an egg!",
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
    emote + a funny conversational reply), 'color' (recolour the robot), 'recolor' (recolour a
    room object named by `target`), or 'resize' (grow/shrink/reset `target`'s size — the robot
    or a room object). 'chat' and 'action' both run `value`'s canned script; only the speech
    differs. For 'recolor' `value` is the colour; for 'resize' `value` is 'grow'|'shrink'|
    'reset'; `target` names the object (a `TARGETS` key, 'robot' for the pet itself)."""

    kind: str
    value: str
    speech: str
    target: str | None = None


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


def _target_in(t: str) -> str | None:
    """The first room target named in `t` (longest phrases first), or None. Word-boundary
    matched so "wall" doesn't fire on "always"."""
    for phrase, key in TARGETS:
        if _match(t, (phrase,)):
            return key
    return None


def _size_dir(t: str) -> str | None:
    """The size change asked for — 'huge'/'tiny' (jump to max/min) or 'grow'/'shrink' (a step) —
    else None (the reset words are handled separately, since "normal" is also a colour reset)."""
    if _match(t, _HUGE):
        return "huge"
    if _match(t, _TINY):
        return "tiny"
    if _match(t, _GROW):
        return "grow"
    if _match(t, _SHRINK):
        return "shrink"
    return None


def _form_in(t: str) -> str | None:
    """The creature the message asks the pet to become — only when paired with a transform verb,
    so a bare "cat" in chit-chat never morphs the pet. Longest creature words first."""
    if not _match(t, _FORM_TRIGGERS):
        return None
    for word in sorted(FORMS, key=len, reverse=True):
        if _match(t, (word,)):
            return FORMS[word]
    return None


def classify(text: str) -> Intent | None:
    """Match a child's message to a COMMAND only — a resize ("make the bed bigger", "make me
    smaller"), a colour (the robot: "turn red"; a room thing: "turn the floor blue"), or a play
    action ("dance", "chase the ball", "do a fart"). Returns None for everything else —
    greetings, questions, chit-chat — so it flows to the LLM for a *real* conversation (with
    `chat_reply` as the no-LLM fallback). Commands act instantly; talk actually talks back."""
    t = " ".join(text.lower().split())
    if not t:
        return None
    # "Reset everything" FIRST — a full wipe of every ephemeral effect + the pet's own colour.
    if _match(t, _RESET_ALL):
        return Intent(
            kind="reset_all", value="all", speech="Poof! Everything's back to normal! Beep-boop!"
        )
    # Morph FIRST — "change into a dog", "be a dragon", "turn back into a robot". A transform verb
    # plus a creature word (so chit-chat about animals never fires); "robot" is the reset form.
    form = _form_in(t)
    if form is not None:
        return Intent(kind="form", value=form, target=None, speech=form_speech(form))
    target = _target_in(t)
    # Resize next — a huge/tiny/grow/shrink word, or "make <target> normal" (a size reset).
    # Defaults to the robot when no room thing is named ("make it bigger" → the pet grows).
    size = _size_dir(t)
    if size is None and _match(t, ("make",)) and _match(t, _SIZE_RESET):
        size = "reset"
    if size is not None:
        tgt = target or "robot"
        return Intent(kind="resize", value=size, target=tgt, speech=resize_speech(tgt, size))
    # Colour next. A named room thing → recolour that object; otherwise the robot itself (the
    # original behaviour). "turn the floor normal" resets that object's colour (colour "default").
    color = _color_in(t.replace("!", " ").replace(".", " ").split())
    if color is not None:
        if target is not None and target != "robot":
            return Intent(
                kind="recolor", value=color, target=target, speech=recolor_speech(target, color)
            )
        return Intent(kind="color", value=color, speech=color_speech(color))
    # "Clean up your room" (optionally "…and go to bed") — a tidy chore the wall plays out. The
    # compound form chains into bedtime, so it's checked before the plain keyword table (whose
    # "sleep"/"bed" entries would otherwise swallow the bedtime half).
    if _match(t, _CLEANUP):
        if _match(t, _BEDTIME):
            return Intent(
                kind="action",
                value="cleanup_bed",
                speech="Okay! I'll tidy up, then off to bed. Beep-boop!",
            )
        return Intent(kind="action", value="cleanup", speech="Cleanup time! Let me tidy my room!")
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


def recolor_speech(target: str, color: str) -> str:
    """A friendly line for recolouring a room thing (a wall effect, not the robot)."""
    name = _TARGET_NAMES.get(target, target)
    if color == "default":
        return f"The {name} is back to normal!"
    if color == "rainbow":
        return f"Rainbow {name}! Wheee!"
    return f"Ooh, a {color} {name}!"


def resize_speech(target: str, direction: str) -> str:
    """A friendly line for a resize (the robot, or a room thing)."""
    if target == "robot":
        return {
            "grow": "I'm SO big now! Rawr!",
            "huge": "I'm GINORMOUS! RAAAWR!",
            "shrink": "I'm teeny-tiny! Squeak!",
            "tiny": "I'm itty-bitty teeny! Squeeeak!",
        }.get(direction, "Back to my normal size!")
    name = _TARGET_NAMES.get(target, target)
    return {
        "grow": f"Big big {name}! Whoa!",
        "huge": f"GIANT {name}! Whoaaa!",
        "shrink": f"Teeny tiny {name}!",
        "tiny": f"Itty-bitty {name}!",
    }.get(direction, f"The {name} is back to normal size!")


# What the pet says as it morphs — each in the new creature's "voice".
_FORM_SPEECH: dict[str, str] = {
    "robot": "Beep-boop! Robot mode! I'm me again!",
    "dog": "Woof woof! I'm a puppy now! *wags tail*",
    "cat": "Meoww! I'm a kitty! Purr purr purr.",
    "dragon": "RAWWWR! I'm a big flappy dragon! Rawr-hee-hee!",
    "cow": "Mooooo! I'm a cow now! Moo moo!",
    "pig": "Oink oink! I'm a little piggy! Snort!",
    "chicken": "Bok bok bok! I'm a chicken! Bagawk!",
}


def form_speech(form: str) -> str:
    """A friendly line for morphing the pet into a creature (or back to the robot)."""
    return _FORM_SPEECH.get(form, f"Poof! I'm a {form} now!")


def canonical_color(name: str) -> str | None:
    """Fold a colour name/alias onto a known `PET_COLORS` value, or None if unknown."""
    n = name.strip().lower()
    if n in PET_COLORS:
        return n
    return _COLOR_ALIASES.get(n)
