# What the panel answers to

> **Status:** Living · **Last verified:** 2026-09-27 — **brightness has never worked**, on any panel, since August, and after 0.3.16 it still does not — on purpose. The cause was real: the QSPI command framing was missing from the two runtime writes (`0x51` went out as instruction `0x00`, was discarded, and the call returned `ESP_OK`). Framing them in 0.3.16 made brightness work for the first time and took **both panels' render tasks down within minutes** — black screen, no audio cue, no response to touch, recovered only by a power cycle. 0.3.18 backs the framing out, so the writes are inert again and the panel is always at full. **The mechanism is NOT established, and two guesses have already been refuted from ESP-IDF's own source**: `panel_io_spi_tx_param` acquires the bus and drains every in-flight transfer before it transmits, so it is not mid-frame corruption; and `display_set_brightness` only raises a flag for the render task, so it is not a cross-task call. One panel also ran 956 s healthy on 0.3.16 first, so the 30-second re-assert is not sufficient on its own. What IS measured: framed hangs both panels, unframed does not.
>
> **The asymmetry that made it unrecoverable is worth fixing on its own.** `sdkconfig` sets `CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT` with a zero delay, so a CRASH self-heals in seconds; `CONFIG_ESP_TASK_WDT_PANIC` is NOT set, so a task merely BLOCKED prints a warning every 30 s and sits there until a human pulls the cable — and ESP-IDF's `tx_param` waits `portMAX_DELAY` twice. A hung task is the one fault this firmware cannot recover from, on a box whose owner has no terminal. Re-landing brightness needs that watchdog on AND the writes ordered against the blit, on a bench panel with a cable; see `QSPI_CMD` in `firmware/main/display.c`. Two conclusions drawn through that same broken path are now VOID — the `REASSERT_MS` display-on experiment, and "this panel does not answer reads over QSPI". Prior: `tell sister` measured on 0.3.14 with hand-checked phonemes registered and **never fired once** across an afternoon, while `tell dad` fired 9/9. The recipient moves to a 2×2 icon grid in 0.3.15; both phrases stay in the vocabulary. **The grid is opened by a long press on the pet, not by the button** — the button opened it until 0.3.26 and is the power control now: a short press is standby (screen off, microphone off, still reachable, wakes on a message or a finger), and a five-second hold is deep sleep with the button as the only way back. Also measured: the microphone is **clipping** (`mic_peak` 32767 with the codec AGC off), and `raw_string` — the phoneme-decode diagnostic — comes back **empty** on this model, so it cannot answer what was heard.

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
| `tell sister` | **measured 2026-09-27 on 0.3.14: never fired once.** Registered with hand-checked phonemes (`TfL SgSTk`), `vocab_ok: 48, vocab_bad: 0`, nothing refused. Two children tried it across an afternoon; every one of the nine messages that got out went to Dad. The transcripts caught them routing around it — one message to Dad reads *"Tell Dad. It's doing it. So now you say your message."* |

**`tell sister` is why the 2×2 grid exists** (0.3.15): the recipient stops being something a
four-year-old has to pronounce. The phrase is left in the vocabulary — removing it might free
model capacity for the phrases that do work, but that is a separate change with its own
measurement, and `tell dad` still fires 9/9.

**Two findings from the same afternoon that bear on every phrase here:**

- **The microphone is clipping.** `mic_peak` came back at 32767 — dead-on int16 saturation —
  with the codec's AGC off (`alc: 00 already-off`) and a fixed 30 dB gain. `speech.c` counts
  clipped samples internally but the count never leaves the panel. Distortion hurts longer
  phrases more than short ones, which fits this table better than any hypothesis below:
  everything confirmed working is short, and `tell sister` is the longest command in the table.
  Testable from the PWA with no flash — lower `mic_gain_db`, or turn `mic_agc` on.
- **`raw_string` is empty on this model.** The field 0.3.10 added to tell *"the microphone never
  carried it"* from *"it was heard as something else"* comes back blank on every decode, hit and
  near-miss alike. The firmware reads it correctly (`get_results` straight after `DETECTED`); the
  vendor simply does not fill it for English MultiNet7. `esp_mn_results_t` has a sibling field,
  `string` (*"recognized string **with** commands graph"*), which has not been tried. **So the
  question this table has asked since 2026-09-23 — heard-and-rejected, or never heard — is still
  unanswered, and the instrument built for it does not work.**

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

What answers it is the raw decode `speech.c` already computes when a phrase is heard and the
command graph rejects it — it records near misses as well as hits. The obstacle was never the
measurement; it was the pipe. The `heard` ring held **three** entries and ships with the
15-minute telemetry poll, so a whole play session of children shouting at a panel arrived as the
last three things it thought it heard, and every attempt to look found it empty.

**Widened to twelve in 0.2.91**, with consecutive identical decodes collapsed into one entry and
a repeat count — a television saying one word, or a child saying "burp" eight times because it
is not working, would otherwise flush the ring with eight copies of one fact and push out
everything that explained it. The repetition is itself the signature of a false trigger, so it
is kept rather than spent.

So the next play session after 0.2.91 should answer this. What to look for in the telemetry:
a `burp` entry with `fired: 0` says the recogniser heard it and the command graph rejected the
decode; a raw decode of something else entirely says the pronunciation guess is wrong and
`esp_mn_commands_phoneme_add()` is the lever; nothing at all says the microphone never resolved
it as speech.

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
name/version) · **5 taps then a 5 s hold (restart) · 6 taps then hold (touch
calibration) · 7 taps then hold (swap body)** — moved up from 3/5/4 in 0.3.19 so that a hold
after the handful of taps a child actually produces reaches the "who?" grid instead of
maintenance. A hold at ANY count these three have not claimed opens that grid, which is why
they are as few and as high as they are; `gesture_reserved()` is the single place that says
which counts are spoken for.

## Still untested

`bounce around` / `wake up now` (boing) and `play peekaboo` / `hide your eyes`. Every other
animation has at least one phrase confirmed by a child.
