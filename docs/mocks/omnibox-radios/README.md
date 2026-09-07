# The omnibox radio sheet — which radio, and what it is doing

> **Status:** Living · **Last verified:** 2026-09-07

**The defect.** Tapping the omnibox radio icon always opened the LISTEN sheet.
`HomeScreen` passes only `sdr.listening` and `SdrTunerSheet` has no purpose
handling at all, so a radio doing APRS or Spectrum had nowhere to show — the
owner got a tuner for something producing no audio. `sdr.sessions` already
carries every held radio; nothing was reading it.

This box holds **two** dongles (`09022796`, `77192819`), so they can be on
different jobs at the same time. That is what makes this worth a shape rather
than a patch.

**Reviewed 2026-09-07** under the `PROCESS.md` GUI gate. Four paths were built
and tried on the phone:

| | shape | why not |
|---|---|---|
| `a-swipe-pager.html` | one page per radio, swipe between | the gesture is invisible; two dots are the only sign a second radio exists |
| `b-radio-switcher.html` | a tab per radio | shows both radios, but the sheet stays a window — it cannot change anything |
| `c-both-at-once.html` | every radio stacked, collapsible | fastest read of the box, but each radio gets half the height and a waterfall feels it |
| **`d-radio-then-task.html`** | **radio, then task** | **CHOSEN** |

**D is the binding spec.** Pick the radio; a **Doing** row (Listen / APRS /
Spectrum / Idle) shows what it is on and switches it; that job's live content
follows. It is the Radio tab's own hierarchy — "radio is the object", settled in
`SDR_RADIO_PLAN.md` W4 — brought into the sheet, and the only path where the
omnibox icon is a **control** rather than a window.

**Its cost, accepted with the choice:** two rows of chrome before any content,
and the sheet gains a way to stop a running job. Guard the destructive edge —
switching a radio that is mid-job ends that job, and the sheet must say so
before it does.
