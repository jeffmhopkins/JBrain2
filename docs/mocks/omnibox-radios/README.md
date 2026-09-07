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

**Shipped 2026-09-07** as `frontend/src/components/SdrRadiosSheet.tsx`. What the
implementation settled beyond the mock:

- The tab row is drawn **only when more than one radio has a tab** — with a
  single dongle it offers a choice that does not exist, and D's accepted cost
  (chrome before content) is then paid for nothing. A radio the USB scan cannot
  see still gets a tab when a service is dedicated to it: that job waits for its
  dongle rather than moving, so it is still the radio the job is on.
- The **Doing** row is not reimplemented. The sheet mounts `RadioJob`, the same
  component the Radios tab uses, so the release-then-take, the two-step arming
  for a job that needs a band, and the confirm the cost note demanded ("that
  stops APRS on this radio — tap again") come with it rather than being written
  a second time and drifting.
- The APRS **health line** is not drawn here: the sheet fetches no log, because
  a poll behind the composer for one line is not worth it. The way to the log —
  the Radio screen, which owns that poll — is the surface's own button.
- The omnibox icon's condition moved from `listening !== null` to
  `anyHeld(sdr)`. `listening` is the one session the icon DRAWS and it prefers
  the tuner, so a box whose only radio was decoding APRS or sweeping showed no
  icon at all — the sheet would have had no way in.
- `SdrTunerSheet`, the wrapper this replaces, is gone; its controls live on as
  `SdrTunerControls.tsx`, mounted by this sheet and by the Radios tab.

**Corrected 2026-09-07, from the owner's own screenshots.** Two tabs over two radios is
a thing the surfaces underneath had never been asked to do, and three faults fell out of
it at once:

- **The picture came back blank.** Switching away and back restarted the waterfall from
  nothing at the row rate — one strip at the bottom of an empty box. A session now keeps
  its recent rows (`listen.HISTORY_ROWS`) and hands them to a viewer as it attaches, so
  a picture that has been running for minutes comes back as a picture.
- **The tuning strip said "waiting for the radio" under audio that was playing.** The
  spectrum stream served whatever `drawing()` chose, and it PREFERS a spectrum session —
  so with the other dongle sweeping, the tuner's own channel strip was attached to the
  sweep and waited for rows that session does not draw. The stream takes a `serial` now,
  and a named radio drawing nothing is an error rather than somebody else's picture. The
  PWA also stopped letting "the first caller's view" stand: a surface asking for a
  different picture reopens the stream instead of inheriting one.
- **The APRS surface could only offer a link.** Whether the job is working is the one
  thing an owner opens it for, so it shows the last packet heard — callsign, when, and
  the start of what it said — fetched by the surface itself when nothing hands it a log.
  The health line's omission (recorded above as deliberate) stands only where a log is
  already being polled; behind the composer this peek is what replaces it.
