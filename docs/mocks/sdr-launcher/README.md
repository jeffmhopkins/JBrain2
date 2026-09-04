# Radio launcher — GUI gate round 1

> **Status:** Awaiting the owner's choice · **Last verified:** 2026-09-04

`shapes.html` — three shapes of the same screen, on one data model, switchable.
`docs/reference/PROCESS.md` requires three interactive mocks before implementation;
they are one file here rather than three because the whole question is *comparison*,
and flipping between shapes without losing state is the only way to see what each costs.

## The question the owner is answering

**Is the radio the object, or is the job?**

- **A — the radio.** Tabs become `Radios | APRS | Recordings`. The first tab is a roster;
  tapping a radio opens its control layer, and its job (Listen / APRS / Spectrum / Idle)
  is chosen there.
- **B — the job.** Today's tabs stay. Each grows one line: `on [ radio ▾ ]`.
- **C — one console.** No detail level; per-radio panels that expand in place.

**A re-opens a decided round.** `../aprs/c-single-dongle.html` chose "a switch in the
APRS tab" and explicitly rejected a radio-wide mode selector. That was decided on a
**one-dongle box**; the second dongle arrived and one session per radio shipped
(`APRS_CONTROL_PLAN.md` P0b), so the premise changed — but whether it changed *enough*
is the owner's call, which is why A is presented rather than assumed.

## Settled, and identical in all three

- **The band picker is the front door.** 32 curated US sections (`jbrain/sdr/bands.py`),
  each carrying its own mode, step, channel spacing and sweep settings. Manual entry is
  the last item in that sheet, not a control on the main surface.
- **The tuning cluster is the same component as the omnibox sheet**, in the same order.
  DESIGN.md forbids the sheet from growing, so launcher-only controls stack strictly
  *below a hairline* — never woven in. Two surfaces that look alike and behave
  differently is the failure this guards against.
- **Expert settings report themselves when collapsed** — `gain auto · 15 kHz · squelch
  off` — so a non-default is visible without opening anything.

## What the harness proves rather than asserts

Nine state buttons, and all three shapes redraw from the same store: both radios working,
one radio only, a dedicated radio unplugged, a sweep running, a receiver gone deaf, gain
changed by hand, tuned to shortwave, the USB scan unreachable, nothing plugged in.

Three behaviours are drawn from measurement, not decoration:

- **Shortwave has no gain control** — the tuner is powered down below 24 MHz — and
  **cannot be swept**, because `rtl_power` hardcodes the ADC branch this hardware does
  not wire. Open Advanced on a shortwave section and it says so.
- **Dedication binds the tuner too.** Long wire is reserved for APRS, so its other jobs
  are disabled with the reason on the control. Unplug it and APRS *waits*.
- **A sweep is a run, not a mode.** It holds a radio, counts down, and ends by itself.

## Not decided here

The live spectrum's own surface (P1 is a 1 fps waterfall from streamed `rtl_power`);
**landscape**, which would be this app's first orientation-aware screen and is
deliberately not proposed; and the waterfall's colour ramp, which needs a DESIGN.md
token that does not exist yet.
