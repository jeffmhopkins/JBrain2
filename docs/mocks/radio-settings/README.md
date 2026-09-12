# Radio settings — gain and upconverter

**Status:** proposed · **Last verified:** 2026-09-12

The owner asked for two things, per radio, in settings: control over gain (an
inline amp was driving the front end too high) and a tuning offset for a Nooelec
Ham It Up upconverter.

## The shape is the owner's correction

Three rivals were built first — an inline disclosure on the radio card, a
full-screen page per radio carrying the measured gain ladder as a chart, and a
bottom sheet drawing `antenna → upconverter → amp → tuner gain → ADC` as a
five-bead signal chain. The owner discarded all three with **“needs to be
simpler,”** and that verdict is the design:

**`a-two-more-fields.html`** — not a new surface. The Settings → Radios subcard
that ships today (binding spec `docs/mocks/sdr-dongles/a-named-roles.html`)
already models *the radio and what it is for*, with fields keyed by serial and one
Save per card. Gain and an upconverter are two more answers to that same question.
A new paradigm was solving a problem nobody had.

## What the two fields are, and why they are shaped this way

**Gain** offers the measured rungs and nothing between them, because nothing
between them was measured. On this radio at 162.550 through the fixed listen
chain:

| Gain | 0 dB | 10 | 20 | 30 | 40 |
|---|---|---|---|---|---|
| SNR | 20.2 dB | 40.5 | 41.4 | 39.7 | 32.8 |

The ladder has a **plateau at 10–30** and both ends are worse — the bottom by
~20 dB. So no continuous slider: 0 is not the safe end of a range, it is deaf.
With an inline amp the move is *down* the plateau (10 or 20), because the amp
supplies gain the tuner then does not have to.

**Auto** is on the list because the owner asked for it, and last on the list
because on this hardware it is measurably worse: under AGC, 162.550 grew a
phantom station at 162.35 and a spur comb at ±55.5/111/166/222 kHz — seven
signals that do not exist. Selecting it says so in place, and the waterfall's
scale legend goes from `dBFS @ 30 dB` to `relative — gain is moving`.

**Upconverter** shifts only the hardware tune: to hear 7.200 the dongle tunes
132.200, and every frequency the owner reads stays at 7.200. The offset is
editable because 125 MHz is this unit's number, not every unit's.

## What this deliberately does not claim

HF is **already reachable** without a converter — `TUNABLE_MIN_MHZ` is 0.1 via
direct sampling — so “unlocks HF” is not the argument and is not made. What the
converter actually buys: a gain control on HF at all (direct sampling powers the
tuner down, so gain is a no-op there), no `28.8 − f` mirror summed into every
bin, no 14.4–24 MHz hole, and hopped HF waterfalls. The card states the first,
because it is the one the owner is about to feel.

Nor does it assert a passband edge for the converter. No one has measured this
unit's, so the card warns when a VHF frequency is tuned with the converter
inline rather than naming a number it does not have.

## Open question before build

Whether this Ham It Up has the **hardware bypass switch**. The app cannot see its
position, so with a bypass the setting probably wants three states
(`off · inline · bypassed`) rather than two — otherwise the stored offset is a
claim only the owner can keep true.

## The harness is the argument

Changing the frequency makes the gain field answer honestly: at 7.200 with the
converter off the radio direct-samples, the tuner is powered down, and gain
cannot apply — the control says so and stores the value anyway. Turn the
converter on and gain returns, because the hardware now tunes 132.200 through the
tuner. A gain that reads back as a number no signal passed through is the
recurring failure in this subsystem; the mock refuses to show one.
