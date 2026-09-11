# Per-radio settings — gain and a tuning offset (GUI-gate mockups)

> **Status:** Living · **Last verified:** 2026-09-11

Every radio on the box gets **two settings of its own**: what gain it runs at, and
whether an upconverter sits between it and the antenna. These mockups are the
`docs/reference/PROCESS.md` GUI gate for that surface. **Round open — nothing chosen.**

There is no settings surface on a radio today. `SdrRadiosTab.tsx` gives one a **Doing**
segmented control (`idle | listen | aprs | spectrum`), a Mode row, and a Reset button —
all of it about *what the radio is doing this minute*. What is missing is the layer
underneath: *what this radio is*, which on an SDR is mostly a question about the chain in
front of the tuner.

## What is already true, and what is not

Worth stating up front, because two of the assumptions this round started from are wrong:

- **Per-radio persisted state already exists.** `settings_store.sdr_radios` keys `name`,
  `description` and `role` to the **serial**, in the `sdr_radios` key of `app.settings`,
  edited from Settings → Radios (`SdrRadiosCard.tsx`, binding mock
  `../sdr-dongles/a-named-roles.html`). Gain and offset are **two more fields in that same
  entry** — no new table, no new store, and they inherit the property that made the
  existing three worth having: they survive a re-plug and a change of USB port.
- **HF is already reachable.** `tuner.TUNABLE_MIN_MHZ` is **0.1 MHz**, not the tuner's
  24 MHz floor, and has been since the direct-sampling work: below 24 MHz the R820T2 is
  powered down and the RTL2832U's ADC is fed straight off the antenna (`rtl_fm -E direct2`,
  the Q branch the NESDR SMArt v5 wires). Listening *and* a waterfall both work down there
  — `rtl_power` was deleted in B1 and the I/Q engine sets the branch at runtime. So "the
  API floor rejects HF" is not the problem the upconverter solves. What it actually
  solves is four other things, listed below.

## What the box can actually do, measured

None of this is aspirational, and no mock invents a number it cannot cite.

| Fact | Where |
| --- | --- |
| **The gain ladder on this radio** — 162.550 through the fixed listen chain: 0 dB → **20.2** dB SNR, 10/20/30 → **40.5 / 41.4 / 39.7**, 40 dB → **32.8** | `deploy/sdr/listen.py` `MEASURING_GAIN_DB` |
| Under AGC, 162.3–162.7 found **seven** signals — a phantom at 162.35 plus a spur comb at ±55.5 / 111 / 166 / 222 kHz. Pinned at 30 dB: **one** | `listen.py` `Tuner.tuner_gain_db` |
| The real station read **11 dB** over the floor under AGC, **~29 dB** pinned | same |
| The floor itself wandered under AGC; three pinned sweeps agreed to **0.2 dB** | `listen.py` `MEASURING_GAIN_DB` |
| At 30 dB the strongest FM station reads **−11.2 dBFS** — 19 dB of headroom | same |
| A retune settles in **1.47 ms** under AGC against **0.09 ms** fixed | `deploy/sdr/radio.py`, `soapy-probe` 2026-09-06 |
| **Gain is a no-op under `direct_samp`** — every gain stage an R820T2 has is inside the tuner, and direct sampling powers the tuner down | `radio.py` `set_gain`, `gain_state` |
| Spectrum and survey already pin **30 dB by construction**; listen and APRS run AGC | `listen.py` `Tuner.tuner_gain_db` |
| The tuner reaches **24–1766 MHz**; the direct path is honest to **14.4 MHz**; **14.4–24 MHz is reachable by neither** | `backend/src/jbrain/sdr/tuner.py` |
| Everything on the direct path arrives **summed with a reversed image of 28.8 MHz − f**, and nothing in software can separate them | `tuner.py` `direct_sampling`, `sdrBands.imageNote` |

### So what does the upconverter actually buy?

Four things, and "reaching HF" is not one of them:

1. **A gain control on HF.** With the signal mixed up to 132.2 MHz the tuner is back in
   the path, so HF gets the same control — and the same comparable dB scale — as VHF.
2. **The mirror goes away.** No more `28.8 − f` folded into the same bins, which is the
   single worst honesty problem the direct path has (a strong 21.4 MHz station currently
   reads as a mystery signal on 40 m).
3. **14.4–24 MHz stops being a hole.** 17 m and 15 m and the 19/16 m broadcast bands are
   refused outright today, by both `whyNotTunable` and `listen.aliased_refusal`.
4. **Wider HF waterfalls.** Below 24 MHz a picture is one capture and cannot hop, so
   `whyNotLive` refuses any section wider than one. Through the tuner, hopping works.

## The two settings, and the trade each one really is

**Gain is one number for every job** — listen, APRS, spectrum, survey — decided by the
owner. That decision has a cost the mocks have to make visible rather than hide: spectrum
and survey are pinned at 30 dB *by construction* today, precisely because a waterfall
whose gain moves has a dB scale that means nothing from row to row. One shared setting
means an owner who picks Auto **gives that up for the waterfall too**. Hence the warning,
and hence its placement being a real design question rather than a footnote.

**Auto is available because it was asked for, and it should not look like the safe
default.** On this hardware it is measurably harmful: it invents four spurs and a phantom
station, costs ~18 dB of apparent SNR on the real one, and wanders the floor between
sweeps. The ladder says the safe zone is a **20 dB-wide plateau from 10 to 30**, that
0 dB is 20 dB *deafer* rather than gentler, and that 40 already overloads.

**The owner's actual symptom is an amp, and the fix is a lower number.** An LNA's gain
lands *before* the tuner's and adds to it. So the control's job is to make a **low fixed
gain** an obvious, reachable move — which is the opposite of a slider whose safe-looking
extreme is at one end and whose "Auto" sits at the top of the list.

**The offset is not a companion to direct sampling — it replaces it.** Two routes through
one antenna socket. Choosing one excludes the other, and the design has to say so at the
moment of choosing rather than in a help note.

**125 is a default, not a constant.** Other converters use other offsets, and a crystal is
a crystal: if WWV lands 3 kHz off, the fix is to trim the stored number. Every mock takes
a free numeric field to 1 kHz, with 125.000 offered as a chip.

## The three shapes

Three different paradigms from DESIGN.md's table, not one design in three skins. All
three carry the same **harness** — a real frequency selector (162.550 / 7.200 / 18.100)
— because "what happens when the setting cannot apply" has to be *demonstrated*, not
asserted in prose. Flip to 7.200 and the gain control goes inert in all three; turn the
converter on and it comes back, because the hardware tune is now 132.200.

### A — `a-inline-panel.html`. Two switches on the card.

*Inline expansion within the list* — the paradigm for row-level detail that doesn't
warrant navigation. A **Settings** disclosure under the radio's state line opens two rows,
Gain and Tuning offset, each collapsed to its current value and expanding in place. The
gain control is the **measured ladder as five rungs** with their SNR under them, plus a
±1 dB nudge that says "between rungs — not measured" when you are off one.

- *At a glance:* a **tail of value tags** under the state line — `30 dB`, `+125.000 MHz` —
  with the Auto tag amber **and reading the word `auto`**, so colour is never the only
  encoding.
- *Cannot apply:* the gain row's value reads `no control` and its body swaps for a dashed
  block explaining the powered-down tuner, with a one-tap jump to the offset row.
- *The waterfall warning:* a **status banner** directly above the picture inside the card,
  carrying a `Pin 30 dB` button.

**Cheapest to build and shortest to reach** — no navigation at all, and everything it
needs is already on the card. Its cost is that there is **no room to argue**: the evidence
for "don't use Auto" shrinks to a two-line note, and the ladder's bars are 60px tall.

### B — `b-radio-page.html`. The radio's own page.

*Full screen with a back chevron* — the paradigm for a primary task. A settings line on
the roster card opens a screen that belongs to one radio. Its bet is that **one gain for
every job is a decision, not a preference**, and a decision deserves its evidence beside
the control: the ladder drawn as a **chart with the 10–30 plateau shaded**, and — the
moment you select Auto — the **two sweeps side by side**, Auto's seven signals against
pinned's one, over the measured floors (−7.4 dBFS vs −29.2). The advice block tracks where
on the ladder you are and offers `Try 12 dB` when an amp is in the description.

- *At a glance:* one settings line read as a sentence — `Gain 30 dB · no offset` — the
  exception carrying both the colour and the word.
- *Cannot apply:* the whole gain section is replaced by a statement that names the
  frequency, the reason, and that the value is **stored and not applied**.
- *The waterfall warning:* **on the scale legend itself.** A scale that cannot be trusted
  says so on its own face (`dB · relative`, amber, with the pin button inside it) rather
  than in a strip above the picture that the eye learns to skip. When the gain is fixed the
  same legend reads `dBFS @ 30 dB` — the warning is the degenerate case of a label that is
  **always** there.
- Gain applies live; the **offset is an explicit Apply**, because changing it retunes the
  radio.

**The only shape with room to make the argument**, and the argument is the point of the
round. Its cost is a journey — two taps out of the roster, and you leave the radio you
were adjusting to adjust it.

### C — `c-signal-chain.html`. The chain.

*Bottom sheet* — the workhorse contextual form. The radio is drawn as the **signal path it
actually is**: antenna → upconverter → amp → tuner gain → ADC, one stage per row, wired
down the left. **Two stages are settings and three are facts** — the antenna and the amp
are read from the radio's existing `description` ("inline LNA"), which Settings → Radios
already stores, so the sheet explains *why* the number should be low without inventing a
third setting to do it.

- *At a glance:* the chain shrunk to a **five-bead strip** on the card, a dashed bead for
  a stage that isn't there, amber for one that needs looking at, and a caption that says
  which in words: `Wire → LNA → 30 dB`.
- *Cannot apply:* the tuner stage is **drawn bypassed** — the wire visibly routes *around*
  it, the box goes dashed, and its own control offers the converter as the way to put it
  back. This is the only shape where direct sampling is a picture rather than an error
  message.
- *The waterfall warning:* the same **chain strip rides above every waterfall**, because
  the picture's dB scale is a property of the chain that produced it. On Auto the strip
  goes amber and reads *"Tuner gain is moving — the dB scale is relative"*; tapping it
  opens the sheet **at that stage**.
- The converter stage also goes amber when the radio is tuned above HF, because a
  converter inline makes it an **HF radio**: VHF never reaches the mixer, whatever the
  dongle is told to tune.

**The best explanation of the system** and the shape that matches how the owner described
their radios in the first place ("I can change out antenna or dedicate one to APRS"). Its
cost is a **new paradigm on a surface that has none** — nobody has to be taught a list of
switches, and someone does have to be taught a chain — plus a sheet over a screen that is
itself reached by drilling into a card.

## Recommendation

**C — the chain — with B's evidence folded into its tuner stage.**

The reasoning, in order:

1. **The owner's problem is a chain problem.** "I turned on the onboard amp and it drives
   the signal too high" is not a question about a number; it is a question about what is in
   front of the number. A and B both show a gain control with an amp mentioned *near* it.
   C is the only one where the amp is a **stage in the path**, positioned before the tuner,
   which is exactly the fact that makes "come down the ladder" the right move. That fact is
   already in the box — the description field says `inline LNA` — and C is the only shape
   that spends it.
2. **Direct sampling stops being an apology.** The recurring failure this whole SDR family
   keeps naming is *a control that reads like a measurement and is fiction* — `set_gain`
   silently doing nothing, `getGain` reporting a stored number no signal passed through.
   A and B handle it with a well-written sentence. C handles it by **drawing the wire going
   around the tuner**, which is the same statement in a form that cannot be skimmed past.
3. **The two settings are one decision, and C is the only shape that shows why.** In A and
   B, "upconverter" and "gain" are two independent switches that happen to share a card.
   They are not independent: turning the converter on is *what gives HF a gain control at
   all*. In the chain that is a visible consequence — a stage appears, and the bypassed
   stage downstream lights back up.
4. **The strip solves the warning properly.** The waterfall warning is the requirement
   most likely to be got wrong, because a banner that appears only in the bad state is a
   banner nobody has learned to read. B's answer is good — put it on the legend, always —
   and C's is the same idea carried further: the chain strip is present on every waterfall,
   so the amber state is a *change to something already being read*, and it is a **door**
   to the stage that caused it.

What C must take from B before it is built: **the evidence**. C's tuner stage currently
argues in a paragraph; it should argue with B's two sweeps and B's shaded plateau. A sheet
has the room — it can scroll, and the stage is already an accordion. If that turns out to
make the sheet too tall, the fallback is **B**, not A: losing the argument is worse than
losing the shortcut.

Where C is weakest and the review should push: the beads. Five 13px squares are a glyph
that has to be learned, and the caption beside them is what actually carries the meaning —
so the honest question is whether the beads earn their space at all, or whether the caption
alone (`Wire → LNA → 30 dB ›`) is the whole glance.

## What I think the brief has wrong

Five things, in descending order of how much they change the build:

1. **"No persisted per-radio state at all" — there is.** `sdr_radios` already keys three
   fields to the serial. Gain and offset belong in that same entry, which also settles
   where the *editing* surface goes: Settings → Radios is described in the app as "name it
   and say what it is plugged into", and an upconverter and an amp are **exactly** what it
   is plugged into. My recommendation is that the store is one, and both places open the
   **same** control — the roster (where the owner is when the signal is wrong) and Settings
   → Radios (where the rest of the radio's identity lives). `RadioJob` is already shared
   between two surfaces for precisely this reason.
2. **"HF becomes reachable" — HF is reachable today.** `TUNABLE_MIN_MHZ` is 0.1 MHz and
   direct sampling works. What the converter buys is a gain control, the removal of the
   `28.8 − f` mirror, the 14.4–24 MHz hole, and hopped HF waterfalls. Those are better
   arguments than the one in the brief, and they are the ones the mocks make.
3. **One gain for all purposes is a regression for the waterfall, and should be stated as
   one.** Spectrum and survey pin 30 dB *by construction* today, with a measurement behind
   it. Honouring a single per-radio setting means an owner who picks Auto silently
   un-pins the instrument. Two ways out, both cheap: either the pin survives as a floor
   ("the waterfall always runs fixed; here is the number it uses"), or the single setting
   stands and the waterfall says what it is running at, always. I would ship the second —
   which is why every mock's legend reads `dBFS @ 30 dB` in the *good* state, not only in
   the bad one.
4. **A stored gain is only comparable against readings taken at the same gain.**
   `listen.py` already stamps `gain_db` on every frame for this reason. So changing this
   setting quietly invalidates comparison with every survey row already recorded. Nothing
   in the brief covers it; the least that is needed is for the picture to carry the gain it
   was drawn at (all three mocks do), and the right answer is probably that a stored survey
   row shows its own gain when it is read back.
5. **An upconverter inline makes that radio an HF radio.** The offset applies to
   everything the radio tunes, but the converter's input passband does not — VHF never
   reaches the mixer, whatever the dongle is told to tune. If the unit has a hardware
   bypass switch, **the app cannot see its position**, so this setting is a claim about the
   physical world that only the owner can keep true. All three mocks now say so when the
   radio is tuned above HF with the converter on; what they deliberately do *not* do is
   assert the passband's edge frequency, because I have no measurement of it. **Two things
   to confirm before building:** the exact model (Ham It Up v1.3 / Plus), and whether its
   bypass switch exists on this unit — if it does, the setting may want three states
   (`off · inline · bypassed`) rather than two.

One smaller thing, inherited rather than decided here: **`0 dB` is not a safe end of the
range**, and a plain slider implies that it is. It is 20 dB deafer than the plateau. Every
mock uses the measured rungs as the primary control for that reason, and none of them
draws a bare continuous slider from 0 to whatever the driver reports.

## House rules every mock keeps

Single-file and fully offline; dark-first with a working light/dark toggle; phone-framed
at 390px; tokens lifted verbatim from `frontend/src/styles/tokens.css` (the only raw hex
outside them is the waterfall's own background, which `sdrWaterfall.ts` owns); tap targets
≥ 44px; `prefers-reduced-motion` honoured; `aria-pressed` on every toggle and
`aria-expanded` on every disclosure; colour always paired with a word. Each was driven end
to end in jsdom — every control clicked twice in both themes, every numeric field pushed
through empty / zero / out-of-range — with no console errors and nothing rendering
`undefined` or `NaN` into the page.
