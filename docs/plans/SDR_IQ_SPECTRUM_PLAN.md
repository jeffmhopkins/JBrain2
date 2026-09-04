# SDR I/Q spectrum — own the samples, and shortwave stops being a special case

> **Status:** Proposed · **Last verified:** 2026-09-04 · **Waves:** F0◻️ F1◻️ F2◻️ F3◻️ F4◻️ F5◻️ F6◻️

> Reconciled with the root `CLAUDE.md` non-negotiables: no LLM call is added (rule 1);
> nothing new is written to disk, so the storage abstraction is untouched (rule 2); no new
> table, so no new RLS surface (rule 3); no new operator control needs a terminal — the
> waterfall, the band picker and the survey are the same PWA surfaces they are today
> (rule 10); the one new dependency lands in `deploy/Dockerfile.sdr` with a build-time
> import gate, and `scripts/dev-setup.sh` needs no change because the sidecar is built,
> not pip-installed into the dev env (rule 8); and this doc is reconciled with
> `SDR_RADIO_PLAN.md` and `bands.py`'s own docstrings in the same PR as the code (rule 9).

Stop parsing `rtl_power`'s CSV and read the radio's **raw I/Q** instead, doing the FFT
here. That is one change to the sidecar's spectrum engine, and it settles four separate
things the current design records as permanent limits.

---

## 1. The finding this plan turns on

`bands.py` and `tuner.py` both state, as a fact about the hardware, that shortwave cannot
be swept:

> `rtl_power -D` hardcodes direct sampling mode 1 (the I branch) while this hardware wires
> the Q branch. Fixing it means patching a C tool, so HF listening works and HF sweeping
> does not.

**That is true of `rtl_power` and false of the sibling tool in the same apt package.**
Verified against the Debian trixie source of `rtl-sdr 2.0.2-2` — the exact version
`Dockerfile.sdr` installs:

| tool | flag | what it passes |
|---|---|---|
| `rtl_power` | `-D` | `verbose_direct_sampling(dev, 1)` — I branch, hardcoded ✗ |
| **`rtl_sdr`** | `-D` | `verbose_direct_sampling(dev, 2)` — **Q branch, hardcoded ✓** |
| `rtl_tcp` | `-D` | `verbose_direct_sampling(dev, 2)` ✓ |
| `rtl_fm` | `-E direct2` | 1 or 2, as asked ✓ |

The mechanism is `librtlsdr.c`: `rtlsdr_demod_write_reg(dev, 0, 0x06, (on > 1) ? 0x90 :
0x80, 1)` — the `> 1` **is** the I/Q ADC swap. `/usr/bin/rtl_sdr` is already in the image.
There is no C patch and no new package: the HF picture is `rtl_sdr -D` piped into our own
FFT.

**The second half is that `rtl_power` can never be fast, under any configuration.**
`rtl_power.c` parses the interval as an int and clamps `if (interval < 1) interval = 1;`.
One second is a floor in the C. `bands.py:72` has always said what the fast tier needs —

```
LIVE_FAST = "fast"  # one hop; rtl_sdr + FFT; ~10 fps; the radio never looks away
```

— and **30-odd sections claim it** (WX, airband, 2 m, GMRS, marine, the lot), while
nothing branches on `live` anywhere: `api/sdr.py` echoes it into the band list and that is
all. Every one of those sections is served today by the same 1 fps `rtl_power` stream as
`LIVE_SLOW`. This plan is not inventing a tier; it is paying a promise the table has been
making since it was written.

## 2. What we own once we own the samples

Four things, and none of them is a new subsystem — they are all consequences of holding
complex samples instead of somebody else's text:

1. **~10 fps instead of 1**, on one hop, at 100% duty cycle. The radio never looks away.
2. **Shortwave becomes viewable and surveyable.** Ten sections that today refuse.
3. **Bin width becomes ours.** Today `live_bin_hz` walks the power-of-two sequence
   `rtl_power` will *grant*, and the band button reports the grant rather than the
   request. With our own FFT the bin width is `rate / N` and we pick `N` — no negotiation,
   no surprise.
4. **True RF power in dBFS**, for the first time. Everything the system calls a signal
   level today is `max(abs(sample))/32768` over **demodulated PCM** (`listen.py:378`) —
   post-discriminator audio loudness, not RF. It is why the squelch at `listen.py:211` is
   a hand-tuned fraction and not a dB figure.

## 3. The toolchain choice

Ranked against: apt/compiler cost, image size, maintenance, how samples reach numpy,
whether it can select direct sampling **mode 2**, and FFT cost.

**Chosen: `rtl_sdr -D` → pipe → numpy.**

| option | new apt/compiler | installed cost | mode 2? | verdict |
|---|---|---|---|---|
| **`rtl_sdr` pipe + numpy** | **none** (`rtl_sdr` already installed) | numpy ~73 MB | ✅ `-D` | **chosen** |
| `pyrtlsdr` + numpy | none | +26 KB or +1.4 MB | ✅ | 2nd — see the landmine below |
| `rtl_tcp` + socket + numpy | none | numpy only | ✅ (runtime cmd `0x09`) | 3rd — three custom protocols |
| SoapySDR + SoapyRTLSDR | apt | — | ✅ | **structurally impossible here** |
| `rtl_power_fftw` | cmake + g++ + libfftw3-dev | build stage | — | dead since 2020-06 |
| csdr / owrx | C++ source build | large | — | not in Debian |
| GNU Radio | apt | **445 MB, 138 packages** | ✅ | to compute an FFT numpy does in 7.6 ms |

**Why it wins: it is the smallest change that meets the goal.** `Session` already spawns a
subprocess, drains stderr, matches `_CANNOT_OPEN`, restarts on retune and keeps subscribers
attached across the restart. `rtl_sdr` slots into that shape with the CSV parser replaced
by a numpy block. `-d <bare serial>` works identically (same `verbose_device_search`).
Direct sampling mode 2 is the *only* mode `-D` offers — which is exactly the one this board
wires.

**Why not SoapySDR, specifically:** Debian trixie's `python3-soapysdr` ships
`_SoapySDR.cpython-**313**-…so`. The sidecar's interpreter is CPython **3.12**. The SWIG
extension is ABI-locked to an interpreter the container does not run, and there is no
SoapySDR on PyPI at all. For a driver whose last release was 2021, rebasing the image to
chase an ABI tag is a bad trade.

**Why not `pyrtlsdr` (the landmine, reproduced rather than read):** 0.4.0 and 0.5.0 bind
`rtlsdr_set_dithering` and seven `rtlsdr_*_gpio_*` symbols **unconditionally** — the
`try/except AttributeError` guards cover only the two tuner-bandwidth calls. Those symbols
exist in the rtl-sdr-blog fork and **not** in osmocom librtlsdr. Installed against system
librtlsdr, `import rtlsdr` raises `undefined symbol: rtlsdr_set_dithering`. The escapes are
a **2023 pin** (0.3.0, which does import cleanly) or `pyrtlsdr[lib]`, which ships a
*second, forked* librtlsdr plus its own vendored libusb — two drivers in one image
disagreeing about device enumeration, **on a two-dongle box**. Neither is comfortable. It
stays 2nd because what it would buy is real: register-write retunes instead of process
restarts.

**The cost we are accepting by choosing 1st:** a retune is a process restart, so panning or
zooming the waterfall goes blank for ~200–500 ms. This is the same cost `rtl_fm` retunes
already pay, through code that is built and debugged.

### The dependency, and the rule it bends

`Dockerfile.sdr` says today:

> No pip install at all: a radio sidecar that pulled a Python SDR stack would be carrying a
> second implementation of what librtlsdr already does.

The **reasoning** is about SDR stacks, and it is still right — it is why SoapySDR, csdr and
GNU Radio are all refused above. The **rule** was written one notch broader than its
reasoning, and numpy is not a second implementation of librtlsdr: it is the FFT librtlsdr
deliberately does not provide. Rewrite the comment to say what it means, name the one
exception and why, rather than working around a sentence.

`numpy` is 16.7 MB down, ~73 MB installed, next to the ~150 MB of ffmpeg already on this
image for the same kind of reason (a real dependency on an opt-in profile, taken rather
than hand-rolling). A pure-stdlib 4096-point complex FFT is 30–80 ms per frame in CPython —
unacceptable on a box that also runs the LLM stack. `apt install python3-numpy` is not an
escape: it targets Debian's python3.13, not the image's 3.12. Pin exactly and gate the
import at build time, as `Dockerfile.rapidocr` already does (`pip install --no-cache-dir
"numpy==2.2.6"`); reusing that pin keeps one numpy version in the fleet rather than two.

### FFT cost

Measured on a 2.80 GHz Xeon, per 100 ms frame of 2.4 MS/s (240 000 complex samples):

| FFT size | full Welch (every sample) | 8 segments/frame |
|---|---|---|
| 1024 | 6.41 ms → 6.4% of a core at 10 fps | 0.17 ms |
| 2048 | 7.17 ms → 7.2% | 0.44 ms |
| 4096 | 7.62 ms → 7.6% | 0.61 ms |

Plus 1.36 ms for u8 → complex64. **The maximum-quality configuration — 4096 bins,
averaging all 58 non-overlapping segments, 10 fps, 100% duty — is ~9 ms per frame, ~9% of
one core.** So take it: there is no reason to sample a subset of segments and reintroduce
the looking-away problem the fast tier exists to remove. (Those percentages are the
research sandbox's, not the owner's box; the direction is safe because theirs is faster,
the absolute figures are not theirs.)

Two implementation notes worth carrying into the code: a 256-entry `float32` LUT indexed by
the raw bytes yields interleaved I,Q that `.view(np.complex64)` reinterprets for free (no
`astype`, no slicing); and batch the FFT as `np.fft.fft(seg2d, axis=1)` — a Python loop is
several times slower. `np.fft` releases the GIL, which matters because the audio pump and
the HTTP threads share this process.

## 4. Shortwave, honestly

Direct sampling is **not** what the repo's comments assume, and getting this right is most
of the value of the wave.

**The samples are still complex.** `rtlsdr_set_direct_sampling` writes `0xb1 = 0x1a` —
"disable Zero-IF mode" **turns the RTL2832U's digital downconverter on** — and
`rtlsdr_set_center_freq` then routes to `rtlsdr_set_if_freq`, writing the frequency into
the DDC's NCO. The chip complex-mixes the real ADC stream and decimates. What arrives over
USB is ordinary interleaved 8-bit complex I/Q, indistinguishable in format from tuner mode.
**Consequence: a plain complex FFT. No `rfft`, no folded half-spectrum, no special case in
`iq.py`.**

**But the underlying stream is real, so a capture is only honest inside a window.** Its
spectrum is conjugate-symmetric about 0 and about ±14.4 MHz, so:

> A capture at centre `fc`, rate `R`, is non-redundant iff **`R/2 ≤ fc ≤ 14.4 MHz − R/2`**.

At 2.4 MS/s that window is 1.2–13.2 MHz, and **two rows in the current table fall outside
it**:

- **`mw`** (0.53–1.70, centre 1.115) at 2.4 MS/s puts its passband at −0.085…+2.315 MHz.
  Everything drawn below 0 Hz is a mirror of just above it, and the ADC's DC offset lands
  in-band under exactly the same condition. **Fix: 1.2 MS/s** → 0.515–1.715 MHz, covering
  the section almost exactly.
- **`20m`** (14.15–14.35, centre 14.25) at 2.4 MS/s runs to 15.45 MHz, past Nyquist,
  folding 14.4–15.45 back onto 13.35–14.4 *within the same picture*. **Fix: 250 kS/s** →
  14.125–14.375.

Legal rates are **225001–300000 Hz** or **900001–3200000 Hz** (`rtlsdr_set_sample_rate`
rejects the rest, identically in direct sampling — it is the same resampler). Both fixes
are legal. This is exactly the kind of arithmetic `validate()` exists to enforce, so it
should enforce it.

**`mirrored` is both dead and mis-stated.** Every section in the table stops at ≤14.35 MHz,
so the flag is `False` for all ten — while *all ten* actually carry a reversed image from
`28.8 MHz − f`. `mw` picks up CB (26.965–27.405 → 1.395–1.835); `40m` and `sw-41m` pick up
13 m broadcast. And nothing anywhere mentions the out-of-HF breakthrough: **FM broadcast
88–108 MHz folds onto 1.6–14.4 MHz** — a strong local at 96.0 lands at 9.6 MHz, mid-31 m
band. Airband 118–137 → 2.8–21.8. The board's LF/MF/HF diplexer attenuates the VHF folds by
an unpublished amount and gives **zero** protection against the 14.4–28.8 MHz images, which
sit inside its passband. No transform can undo any of it: the two contributions are summed
in one bin and are information-theoretically inseparable without an analogue filter.

So the honest UI is not "mirrored above 14.4 MHz". It is, per section, *"carries a reversed
image of X–Y MHz"*, plus one global caveat that strong FM/VHF can appear anywhere on HF.
That is a `image_start_hz`/`image_stop_hz` pair derived in `bands.py`, replacing `mirrored`.

**Gain really is dead below 24 MHz** — the repo is right. `rtlsdr_set_direct_sampling`
calls `dev->tuner->exit(dev)`, and the 0–49.6 dB is *tuner* gain. `demod_args` already
handles this for `rtl_fm`; the spectrum path needs the same rule. With ~7 effective ADC bits
plus ~33 dB of processing gain at N=4096 there is enough range for a useful waterfall, but a
strong MW carrier can clip the ADC and **there is nothing in software to do about it**.

**One claim we will not ship until it is measured.** The two research paths disagree on
whether a signal above 14.4 MHz arrives frequency-inverted. Working the NCO arithmetic
(`if_freq = ((freq * 2^22) / rtl_xtal) * -1`, truncated to 22 bits) says the modulo wrap
cancels the aliasing arithmetic and the *wanted* signal is upright in every zone, with only
the *image* reversed; the classic "even zones invert" rule applies to real one-sided
downconversion, not to a complex DDC. That is derivation, not measurement. F0 settles it.

## 5. Waves

**F0 — the gate. No code.** Two one-shot on-box checks, both prerequisites to writing code
that claims HF works:

- `rtl_sdr --help` shows `-D`. The image pins only `FROM python:3.12-slim`, unpinned by
  suite; **bookworm's `rtl_sdr.c` has no `-D` at all** and its `rtl_fm` has no `direct2`.
  That HF *listening* works today is strong evidence the image is trixie/2.0.2 — this
  confirms it, and F1 pins the suite so it cannot slide backwards silently.
- `rtl_sdr -D -f 20500000 -s 2400000 -n 2400000 -`, FFT it, find WWV's 20.000 MHz carrier.
  **At −500 kHz the signal is upright** and §4's derivation holds; **at +500 kHz it is
  inverted** and the fix is to conjugate above 14.4 MHz. WWV also gives 5, 10, 15 and 25
  MHz free. *(Blocked on a radio being back on the bus — `77192819` is off it and awaits a
  replug or a powered hub.)*

**F1 — numpy, and the rule that bends.** Pin `numpy==2.2.6`; rewrite the Dockerfile comment
to forbid SDR stacks rather than all pip, naming the exception and why; add `command -v
rtl_sdr` to the build-time assertion beside `rtl_fm`/`ffmpeg`/`direwolf`; **pin the base to
`python:3.12-slim-trixie`** so a base-image slide cannot silently remove `-D` from every HF
band. `import numpy` joins the build gate.

**F2 — `deploy/sdr/iq.py`: samples → frames.** Pure and testable with no radio and no
subprocess: u8 LUT → complex64, segment, window, batched FFT, Welch average, dBFS. Tests
drive synthetic tones — a tone at a known offset must land in a known bin at a known level,
and a two-tone case must not smear. This module is what makes the rest safe to write.

**F3 — the `spectrum` purpose switches engine.** `_spectrum_cmd` becomes `rtl_sdr` where
the section qualifies; the pump becomes a byte reader feeding `iq.py`. **The `Frame`
contract does not change** (`at`, `start_hz`, `bin_hz`, `db[]` — self-describing, which is
why a retune needs no protocol event), so the SSE stream, `sdrSpectrum.ts` and the canvas
are untouched. The frontend is already done.

One thing here is not optional: **`rtl_sdr`'s overruns are silent to the picture.** If the
reader stalls, librtlsdr's ring (15 × 262144 ≈ 3.9 MB ≈ 820 ms of slack) overflows,
`rtl_sdr` complains on **stderr**, and the waterfall looks completely normal — the noise
floor is fine, because we are transforming the samples that *did* arrive. A waterfall
dropping a third of its time base would read as a quiet band. Drain that stderr and surface
it as a condition on the stream, the same way `dutyNote` surfaces hop cost. A picture that
hides what it missed is the exact failure this whole surface is built against.

**F4 — the band table learns the truth.** Per-section `sample_rate_hz` with the Nyquist
window enforced in `validate()`; `surveyable` refactored from "cannot be swept" to *which
engine covers it*; `mirrored` replaced by `image_start_hz`/`image_stop_hz`;
`LIVE_FAST_MAX_HZ`'s rationale (the R820T2 IF rolloff) noted as **not applying below 24
MHz**, where the tuner is powered down. `mw` → 1.2 MS/s, `20m` → 250 kS/s.

**F5 — HF goes live.** `tuner.sweepable`/`viewable` stop refusing below 24 MHz;
`sdrBands.ts:whyNotLive` stops saying "shortwave cannot be swept"; the per-section image
caveat and the "no gain control here" note appear where the owner will read them. Ten
sections that refuse today start working.

**F6 — stop calling loudness a signal level.** The spectrum path reports true dBFS off the
FFT. The listening path keeps `rtl_fm`, so its `peak` stays audio loudness — but it gets
**labelled** as such instead of presented as signal strength, in the API field docs and in
the UI. Unifying them means replacing `rtl_fm` with a numpy demodulator, which is §6.

## 6. Deliberately not in this plan

**Replacing `rtl_fm` with a numpy demodulator.** It is affordable — NFM demod measures
~1.5 ms per 100 ms of I/Q, 1.5% of a core — and it would give one radio spectrum *and*
audio at once, plus one true dB scale everywhere. **The blast radius is why not:** that PCM
is the substrate for `_peak`, the segment cutter, whisper captions, the direwolf feed and
the recordings library. Six features to re-validate to gain simultaneity we can already buy
for $30 — **the lease is already per-radio**, so listen on A and watch spectrum on B works
today with no new DSP. On a one-dongle box it stays a mode switch with a useful 409. Worth
revisiting; not worth bundling.

**A real scanner** (fast per-channel power off the same I/Q) becomes cheap once F2 exists,
and is a follow-on rather than a hidden extra here.

**The multi-job-per-radio question.** One radio serving several jobs inside one 2.4 MHz
window breaks the "one radio, one job" assumption `roles.py` and the sidecar's per-radio
409 are built on. That is an architecture decision, and it should be taken deliberately
rather than as a side effect of an FFT.

**A pre-existing bug this makes visible, and does not fix:** `listen.py` applies `-E
direct2` to everything below `MIN_HZ` (24 MHz), so 14.4–24 MHz is currently tuned into the
second Nyquist zone. No table row lives there, so nothing is wrong today; F4's window rule
is what keeps it that way.
