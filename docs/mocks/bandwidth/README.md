# Filter bandwidth — the control that rejects the neighbour (GUI-gate mockups)

> **Status:** Living · **Last verified:** 2026-09-09

The Listen sheet gains a way to say **how wide the radio listens**. These three
mockups are the `docs/reference/PROCESS.md` GUI gate for that control; the
demodulator side is specified in `../../plans/SDR_BANDWIDTH_PLAN.md`.

## Why this exists, measured

The owner reported a station at 5 MHz picking up a neighbouring AM broadcaster.
It is not a tuning error and not the antenna — it is the filter. AM's channel
filter is fixed at **±8 kHz (16 kHz wide)**, so a station 5 kHz away is
comfortably inside the passband when it reaches the envelope detector.

Measured on the real chain (two equal AM carriers 5 kHz apart, the wanted one
modulated at 1 kHz and the neighbour at 1.7 kHz, reading how far the neighbour's
tone lands below the wanted one in the audio):

| Channel filter | Neighbour at +5 kHz |
| --- | --- |
| **16 kHz — today's default** | **0.0 dB — exactly as loud as the station you tuned** |
| 8 kHz | 14.8 dB down |
| 6 kHz | 85.2 dB down |
| 4 kHz | 110.0 dB down |
| 3 kHz | 103.1 dB down |

Two things follow, and they are separate changes:

1. **16 kHz is a defect, not a preference.** AM's audio is already low-passed at
   4 kHz, so the outer half of that filter cannot contribute any wanted audio —
   measured, 16 kHz and 8 kHz give *identical* response at every tone to 3.5 kHz.
   It is 8 kHz of pure interference intake. The default becomes 8 kHz, which
   costs nothing and is the widest rung on the ladder.
2. **6 kHz and below is the owner's call**, because narrowing past the audio
   ceiling does start costing treble — hence a control rather than a new constant.

The filter runs **before** the envelope detector, which is why this works at all:
detection is non-linear, so interference that reaches it intermodulates into the
passband and no amount of audio filtering afterwards can take it back out.

## What all three share

**The filter is drawn on the same picture as the interference.** The tuning
strip already shades the demodulator's passband (`passband_hz` on every frame),
so whatever the control does, the shaded box moves with it and the owner can see
the neighbour fall outside. No mock invents a new picture.

The ladder is per-mode, and wide FM has no control at all — a broadcast FM
station is ~180 kHz wide, so narrowing clips the deviation and distorts rather
than cleans.

| Mode | Ladder (full channel width) |
| --- | --- |
| AM | 8 / 6 / 4 / 3 kHz |
| NFM | 16 / 12.5 / 8 kHz |
| USB, LSB | 3.1 / 2.4 / 1.8 kHz |
| Wide FM | none — fixed at 180 kHz |

## The three shapes

**A — `a-preset-row.html`. Bandwidth is a row under mode.** A second segmented
row, the same shape as the MODE row above it. One tap, nothing hidden, and the
ladder re-labels itself per mode. Carries the measured rejection readout so the
number is on screen next to the choice. *Costs a row of vertical space in a
sheet that is already tall.*

**B — `b-filter-on-the-picture.html`. Drag the filter on the picture.** No new
row: the shaded box *is* the control, with handles that snap to the ladder.
Direct manipulation — you drag an edge until the neighbour is outside, rather
than choosing a number and hoping. *Needs a steady finger on a phone, and the
ladder is invisible until you touch the radio.*

**C — `c-mode-carries-width.html`. The mode button carries the width.** Each
mode button shows its current width; tapping the mode you are already on opens
its ladder in a popover. Shortest sheet of the three, and each rung is labelled
with what it is *for* (shortwave, crowded, DX) rather than being a bare number.
*Discoverability is the weakness — nothing says the second tap does anything.*

All three are single-file and fully offline, dark-first with a working
light/dark toggle, phone-framed, tokens-only (no raw hex outside the token
sheet), ≥44px targets, `prefers-reduced-motion` honoured, and keyboard-operable
(B's handles are `role="slider"` with arrow-key support).
