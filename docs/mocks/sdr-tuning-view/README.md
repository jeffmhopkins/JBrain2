# SDR tuning view — a narrow spectrum on the Listen screen (GUI-gate mockups)

> **Status:** Living · **Last verified:** 2026-09-05

Live canvas: <https://claude.ai/code/artifact/b71379db-f9ee-41f8-bc26-f8e2ac14e70e>

Four static artboards for a **tuning aid on the Listen surface**: a short spectrum
strip, centred on the tuned frequency, spanning twice what the demodulator hears.
Owner's ask, 2026-09-05: *"can listen have a narrow spectrum view that's signal bw\*2
but centered? would help with tuning"*.

Open any `.dc.html` in a browser — each is a standalone 390×844 phone frame — or the
canvas above for all four side by side with the design notes.

## The span rule

**Span = 2 × the demodulator passband**, which makes the shaded band *always* the
middle half of the strip. That fixed reference is the whole point: "am I centred?"
becomes a shape question rather than a number one, and the answer looks the same on
every band.

The passband is what `rtl_fm` is actually given (`listen.py`), not the band plan's
channel raster:

| Mode | `rtl_fm -s` | Passband | View |
| --- | --- | --- | --- |
| `fm` / `am` / `usb` | `AUDIO_RATE` | 16 kHz | 32 kHz |
| `wbfm` | `WBFM_SAMPLE_RATE` | 192 kHz | 384 kHz |

The alternative considered was 2 × the section's `channel_hz` (50 kHz on 2 m, 400 kHz
on the broadcast dial). It keeps the adjacent channel on screen, but the shaded
fraction then varies by band and the fixed middle-half reference is gone.

## The artboards

| File | Shows |
| --- | --- |
| `Main.dc.html` | 146.940 NFM, signal centred — the resting state. |
| `Offtune.dc.html` | The same signal 6.2 kHz high, a third of it outside the passband, with the amber caret and a **Centre it** action. |
| `Wide.dc.html` | 96.5 MHz WBFM: the 200 kHz neighbours reach both frame edges, so tuning between two stations is visible. |
| `Unavailable.dc.html` | What the box can do **today** — see below. |

`Offtune` is the artboard that argues for the feature: 6.2 kHz off a 25 kHz channel is
not audible as a fault (the audio just sounds thin), but half a signal outside the
shading is unmissable.

## The open decision: one radio, one job

A picture and a sound cannot come off the same dongle. Listening runs `rtl_fm`; a
spectrum runs the I/Q engine; the lease is per-dongle. So this feature is one of:

1. **The second dongle** (`Unavailable.dc.html`) — available today, and it is what
   77192819 would stop doing APRS for. Honest, cheap, and the strip goes dark whenever
   the other radio is busy.
2. **Demodulate in numpy inside the I/Q engine** — one capture feeds both, so the strip
   is always there. `docs/plans/SDR_IQ_SPECTRUM_PLAN.md` §8 defers this deliberately:
   the PCM it would replace feeds the segment cutter, whisper captions, direwolf and
   recordings.

Nothing here commits to either. The mock draws (1) because it is the state the box can
actually reach, and the strip is identical under (2).

## Regenerating

The traces are synthesised, not drawn — `generate.mjs` builds all four artboards from
signal shapes, a dB window and a seeded PRNG, so a diff of the `.dc.html` files is a
design change and never noise.

```
node docs/mocks/sdr-tuning-view/generate.mjs
```
