# What the panel answers to

> **Status:** Living · **Last verified:** 2026-09-23

Every phrase the room-endpoint pet recognises, and — the part that is not in the source —
**which ones have actually been confirmed working by a child saying them out loud.**

The table lives in `firmware/main/vocab.c`; this file is the field record beside it. It exists
because a session listed the pet's INTERNAL animation names as if they were sayable, the owner
tried them with the twins, and six of the eight "broken commands" that came back were words
that had never been in the vocabulary at all. A list of what the thing is called is not a list
of what it answers to, and only one of those is useful in a bedroom.

---

## Confirmed working (owner, with the twins, 2026-09-23, firmware 0.2.87)

| Phrase | Does |
|---|---|
| `fart` | the rude noise — five variants |
| `jump` | jump |
| `eat` | eat |
| `kick` | kick |
| `spin` | spin |
| `do a dance` | dance |
| `do a burp` | burp |
| `come and boogie` | bop |
| `wiggle your body` | shimmy |
| `nod your head` | nod |
| `go to sleep` | sleep |

**Every multi-word phrase tested worked — six for six.**

## Confirmed NOT working

| Phrase | Note |
|---|---|
| `dance` | registers fine; never fires. `do a dance` works, so the animation is sound |
| `burp` | same — `do a burp` and `can you burp` reach it |

Both are in the table and both are accepted by the recogniser: the panel's own telemetry
reports `vocab_ok: 46, vocab_bad: 0`, so this is not a registration failure. They are heard and
not resolved.

**Three hypotheses were raised and all three died on the evidence**, which is why this is
recorded as open rather than explained:

- *Short words are fragile* — `eat` is two phonemes and works; `burp` is three and does not.
- *A bare word is shadowed by the longer phrase containing it* — `fart`/`can you fart`,
  `jump`/`do a big jump`, `kick`/`give it a kick`, `spin`/`do a spin` all coexist and the bare
  form works in every one.
- *The animation is broken* — `do a dance` and `do a burp` both play.

What would answer it is the raw decode `speech.c` already computes when a phrase is heard and
the command graph rejects it. It goes only to the serial console, which the owner has no way to
reach (`CLAUDE.md` #10), because the `heard` ring that would carry it to the box holds **three**
entries and ships with the 15-minute telemetry poll. Widening that ring is the prerequisite for
fixing this, not an optimisation.

`esp_mn_commands_phoneme_add()` (beside the plain-text `esp_mn_commands_add()` the firmware
uses) is the lever if the cause turns out to be the built-in pronunciation guess; phonemes come
from `managed_components/espressif__esp-sr/tool/multinet_g2p.py`.

## Not commands, though a four-year-old will say them

These are ANIMATION names, not phrases. Saying them does nothing, and the twins reached for
every one of them:

| They say | The phrase that works |
|---|---|
| bop | `come and boogie` |
| boing | `bounce around`, `wake up now` |
| nod | `nod your head` |
| wiggle | `be silly` (and `wiggle your body` → shimmy) |
| peekaboo | `play peekaboo`, `hide your eyes` |
| sleep | `go to sleep` |

**This is the real finding, and it is a design problem rather than a bug.** The children reach
for the short natural word every time; six animations have no short form at all, and two of the
short forms that do exist are the two that fail. The obvious answer — add six more bare words —
is the wrong one on the evidence above: the one-word form is the unreliable one, and every
always-on single word widens the false-trigger surface on a recogniser already firing at
p≈0.15 (§10.4, the confidence floor). Fix the diagnosis first.

---

## The full vocabulary

48 phrases as of 0.2.88 (46 on 0.2.87 — the two `send` phrases are W3). Source of truth is
`firmware/main/vocab.c`; the host suite enforces the three rules in `vocab.h` (lowercase and
spaces only, two words minimum except a named list, and no phrase a prefix of another).

**Conversation** — `hey fish` (opens the microphone; the reply reopens it for 2 s, up to six
turns) · `stop stop` (ends it, drops the recording)

**Messaging** (0.2.88) — `send a message` (the other panel) · `send dad a message` (the PWA)

**Body** — `change into merc` · `be an ostrich` · `change into robot` · `be a robot`

**Actions** — bare: `dance` `jump` `burp` `fart` `wave` `shake` `laugh` `eat` `kick` `spin` ·
longer: `do a dance` `come and boogie` `wiggle your body` `say hello` `bounce around`
`nod your head` `be silly` `make me laugh` `play peekaboo` `hide your eyes` `go to sleep`
`wake up now` `do a burp` `make a rude noise` `can you burp` `can you fart` `do a big jump`
`have a snack` `give it a kick` `do a spin`

**Colour** — `change your color` · `pick a new color` · `turn` + red/blue/green/yellow/orange/
pink/purple/white. *"color", not "colour": the phrase is fed to an American pronunciation
lexicon, not to a reader.*

**Not voice** — poke the pet (reaction by zone, cycles colour) · press and hold 0.7 s on the pet
(same as `hey fish`) · touch while listening (cancels and discards) · tap the name label (flips
name/version) · 3 taps then a 5 s hold (reboot) · 4 taps then hold (swap body) · 5 taps then
hold (touch calibration).

## Still untested

`bounce around` / `wake up now` (boing) and `play peekaboo` / `hide your eyes`. Every other
animation has at least one phrase confirmed by a child.
