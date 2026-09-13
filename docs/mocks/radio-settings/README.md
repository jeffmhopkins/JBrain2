# Radio settings — gain and upconverter

> **Status:** Living · **Last verified:** 2026-09-12

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

## What this claims, and what it deliberately does not

HF is **already reachable** without a converter — `TUNABLE_MIN_MHZ` is 0.1 via
direct sampling — so “unlocks HF” is not the argument and is not made. What the
converter actually buys: a gain control on HF at all (direct sampling powers the
tuner down, so gain is a no-op there), no `28.8 − f` mirror summed into every
bin, no 14.4–24 MHz hole, and hopped HF waterfalls. The card states the first,
because it is the one the owner is about to feel.

It did not assert a passband edge for the converter either, because no one had
measured this unit's. **The owner supplied it on 2026-09-12: this Ham It Up
passes 300 Hz to 65 MHz.** They own the unit, so that is the number — and it is
now a refusal rather than a warning. `tuner.CONVERTER_MIN_MHZ` /
`CONVERTER_MAX_MHZ` carry it, and a dial outside it with the converter inline is
refused naming both the dial and the tune it would have produced. (The mock's own
harness guessed the edge at 30 MHz, in `a-two-more-fields.html`; it is left as the
design record it is, and 65 is the number the code uses.)

What made this worth fixing rather than documenting: the owner set Inline /
125 MHz and swept 88-108, and every check passed. 98 + 125 is 223 MHz, which the
dongle tunes perfectly — nothing anywhere asked whether the converter's input
had passed the 98.

## Built, and what shipped instead of a switch

**Shipped 2026-09-12.** Both fields live on the existing `sdr_radios` settings entry
beside name/description/role — no new table and no migration — and the card is
`frontend/src/components/SdrRadiosCard.tsx`.

Two departures from the mock's markup, neither of them a departure from its design.
The converter's toggle is a two-state **segmented control** (`Off · Inline`) rather
than an iOS-style switch, because this PWA has no switch component and inventing one
for a two-state choice would be the over-building the owner rejected — `.seg-row` is
what every other two-state choice on these screens already uses. And the mock's
frequency harness has no counterpart in Settings, which knows nothing about what is
tuned: the notes that the harness made frequency-conditional are stated as standing
facts instead ("below 24 MHz with no converter … this cannot apply there"), which is
also the honest form, since the setting outlives any one tuning.

Where the gain becomes visible is the **waterfall's own note**, which now always ends
with the scale it was drawn at — `dBFS @ 30 dB`, `relative — gain is moving` under
auto, or `dBFS (no gain stage)` on the direct path. That last state is a bug this work
closed rather than a label it added: a measuring session asks for 30 dB whatever the
band, so every shortwave row used to carry `gain_db: 30` with the tuner powered down.

## Open question, still open

Whether this Ham It Up has the **hardware bypass switch**. The app cannot see its
position, so with a bypass the setting probably wants three states
(`off · inline · bypassed`) rather than two — otherwise the stored offset is a
claim only the owner can keep true. Shipped with two states; the passband edge
the note left blank is filled in now (300 Hz - 65 MHz), so what is still unknown
is only whether a bypass switch can make the stored offset a lie.

## The harness is the argument

Changing the frequency makes the gain field answer honestly: at 7.200 with the
converter off the radio direct-samples, the tuner is powered down, and gain
cannot apply — the control says so and stores the value anyway. Turn the
converter on and gain returns, because the hardware now tunes 132.200 through the
tuner. A gain that reads back as a number no signal passed through is the
recurring failure in this subsystem; the mock refuses to show one.
