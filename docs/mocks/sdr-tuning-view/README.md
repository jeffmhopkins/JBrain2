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

## One radio does all three

An earlier draft of this README argued that a picture and a sound could not come off
one dongle, and offered the second radio or a deferred numpy demodulator as a fork.
That was wrong, and the owner said so: *"I feel like you can pull off spectrum and
audio from one radio — this is done all the time."* They are right. Every SDR
application does exactly this. The obstacle was never the hardware — it was that
`rtl_fm` is a separate process that opens the dongle exclusively, so **"one radio, one
job" was a constraint this codebase imposed on itself.**

`deploy/sdr/demod.py` removes it. It takes the same complex samples the waterfall is
drawn from and emits the same s16le mono PCM at 16 kHz that `rtl_fm` wrote, so the
level meter, the segment cutter, whisper captions, the MP3 encoder and the direwolf
feed are all unchanged — they sit downstream of a single `read` in
`listen.Session._pump_pcm`. Measured on a 100 ms frame of 2.4 MS/s:

| | demod | + wideband FFT | + zoom FFT | total |
| --- | --- | --- | --- | --- |
| narrow FM / AM | 5.4% | 4.5% | 0.1% | **10.0% of one core** |
| wide FM | 11.7% | 4.5% | 0.1% | **16.3% of one core** |

The strip is better than free. `Audio.baseband` is the decimated complex stream, so a
512-point FFT of it resolves **94 Hz** — six times finer than a 4000-bin transform of
the whole 2.4 MHz capture, at 0.15 ms. The narrow view is not a zoom into the wideband
row; it is a sharper picture that the wideband row cannot produce.

`Unavailable.dc.html` stays as the **interim** state: until that engine replaces the
`rtl_fm` subprocess in `listen.py`, the strip really does need the second radio.

## Regenerating

The traces are synthesised, not drawn — `generate.mjs` builds all four artboards from
signal shapes, a dB window and a seeded PRNG, so a diff of the `.dc.html` files is a
design change and never noise.

```
node docs/mocks/sdr-tuning-view/generate.mjs
```
