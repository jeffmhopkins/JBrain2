"""Grapheme-to-phoneme for the room endpoint's wake phrase.

WHY THE BOX DOES THIS AT ALL. MultiNet7 English — the recogniser on the panels — decodes
PHONEMES rather than words: the shipped model's own `vocab` file is a language model over the
single-letter phoneme classes Espressif's `tool/multinet_g2p.py` emits. Their documentation says
to run that tool over your commands and warns that skipping it calls an internal converter at
runtime "with potential accuracy reduction".

Every phrase in the firmware's table is converted at build time and shipped in flash
(`firmware/main/vocab.h`). Exactly one cannot be: the WAKE PHRASE carries the pet's name, the
owner can change that name from the PWA, and a name that does not exist when the firmware is
built cannot have been converted then. So the single most important phrase on the panel — the
one that starts every conversation — was the only one left on the path the vendor warns about.

The box knows the name. The box can convert it. It rides the settings poll that already runs
every three seconds (`GET /endpoint/settings`), so the panel gets the name and its phonemes in
the same answer and registers the wake phrase the same way as everything else.

WHAT THIS DOES NOT DO, stated plainly because it decides what the next step can be. CMUdict is a
dictionary, not a model: it knows words, and an INVENTED NAME is not a word. Measured
2026-09-26 — `fish` (the name in use) is present, and so are `blink`, `merc`, `nessa` and
`bluey`; `elora` and `lydian` are NOT. An unknown name returns None here and the panel falls
back to its own runtime converter, which is exactly today's behaviour and so cannot regress.
Closing that gap needs a real G2P with out-of-vocabulary handling — `g2p_en` carries a neural
net for precisely this — and that is a heavier dependency than a data wheel. It is also what
per-panel sibling names ("tell Elora") would need, so it is worth doing deliberately rather
than as a rider on this.
"""

from __future__ import annotations

from functools import lru_cache

# The value half of the map in esp-sr's `tool/multinet_g2p.py`, transcribed. ARPAbet with stress
# markers on the left, the model's single-letter classes on the right; stress variants of the
# same vowel deliberately collapse to one class, which is the tool's own behaviour rather than a
# simplification made here.
_ALPHABET: dict[str, str] = {
    "AE1": "a",
    "AE0": "a",
    "AE2": "a",
    "N": "N",
    " ": " ",
    "OW1": "b",
    "OW0": "b",
    "OW2": "b",
    "V": "V",
    "AH0": "c",
    "AH1": "c",
    "AH2": "c",
    "L": "L",
    "F": "F",
    "EY1": "d",
    "EY2": "d",
    "EY0": "d",
    "S": "S",
    "B": "B",
    "R": "R",
    "AO1": "e",
    "AO2": "e",
    "AO0": "e",
    "D": "D",
    "EH1": "f",
    "EH2": "f",
    "EH0": "f",
    "IH0": "g",
    "IH1": "g",
    "IH2": "g",
    "G": "G",
    "HH": "h",
    "K": "K",
    "W": "W",
    "AY1": "i",
    "AY2": "i",
    "AY0": "i",
    "T": "T",
    "M": "M",
    "Z": "Z",
    "DH": "j",
    "ER0": "k",
    "ER1": "k",
    "ER2": "k",
    "P": "P",
    "NG": "l",
    "IY1": "m",
    "IY0": "m",
    "IY2": "m",
    "AA1": "n",
    "AA2": "n",
    "AA0": "n",
    "Y": "Y",
    "UW1": "o",
    "UW2": "o",
    "UW0": "o",
    "CH": "p",
    "JH": "q",
    "ZH": "r",
    "SH": "s",
    "AW1": "t",
    "AW2": "t",
    "AW0": "t",
    "OY1": "u",
    "OY2": "u",
    "OY0": "u",
    "TH": "v",
    "UH1": "w",
    "UH0": "w",
    "UH2": "w",
}


@lru_cache(maxsize=1)
def _dictionary() -> dict[str, list[list[str]]]:
    """CMUdict, loaded once. Imported lazily so a box that never flashes a panel never pays."""
    import cmudict

    return cmudict.dict()


def phonemes_for(text: str) -> str | None:
    """The phoneme-class string for `text`, or None if any word is unknown.

    ALL OR NOTHING, on purpose. A phrase half-converted is worse than one not converted at all:
    the panel would register a pronunciation for part of what it is listening for, and the
    failure would look like poor recognition rather than a missing dictionary entry. None sends
    the whole phrase back to the panel's own converter, which is where it was already.
    """
    if not text or not text.strip():
        return None
    out: list[str] = []
    for word in text.lower().split():
        pronunciations = _dictionary().get(word)
        if not pronunciations:
            return None
        # The first pronunciation, which is CMUdict's own primary. Choosing between variants
        # needs evidence this file does not have, and `g2p_en` makes the same choice.
        encoded = "".join(_ALPHABET.get(p, "") for p in pronunciations[0])
        if not encoded or any(_ALPHABET.get(p) is None for p in pronunciations[0]):
            # A phoneme outside the map means the transcription above has drifted from the
            # tool. Refusing is right: a silently dropped phoneme is a wrong pronunciation.
            return None
        out.append(encoded)
    return " ".join(out) if out else None
