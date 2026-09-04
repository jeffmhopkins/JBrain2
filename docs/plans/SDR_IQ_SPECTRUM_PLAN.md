# SDR I/Q spectrum — own the samples, and shortwave stops being a special case

> **Status:** Proposed · **Last verified:** 2026-09-04 · **Waves:** F0◻️ F1◻️ F2◻️ F3◻️ F4◻️ F5◻️ F6◻️ F7◻️ F8◻️

> Reconciled with the root `CLAUDE.md` non-negotiables: no LLM call is added (rule 1);
> nothing new is written to disk (rule 2); no new table, so no new RLS surface (rule 3);
> every operator control stays the PWA surface it is today (rule 10). Rule 8 is the one
> this plan got WRONG in its first draft and now states correctly: `supervisor`'s tests
> load `deploy/sdr/*.py` **by path**, so a sidecar dependency IS a dev-env dependency —
> `numpy` lands in `supervisor/pyproject.toml`'s dev extra with a regenerated `uv.lock`,
> and `scripts/dev-setup.sh` is updated in the same PR. Rule 11 shapes the wave order:
> `deploy/sdr/` is linted and typechecked by nothing and tested by `supervisor`'s pytest,
> so every wave here is verified from `supervisor/`.

Stop parsing `rtl_power`'s CSV and read the radio's **raw I/Q** instead, doing the FFT
here. That is one change to the sidecar's spectrum engine, and it settles four separate
things the current design records as permanent limits.

**This is the second draft.** The first was independently reviewed and the physics held
up while almost every claim about *downstream* code did not. §6 exists because of that
review, and the corrections are marked ⟲ throughout rather than quietly folded in.

---

## 1. The finding this plan turns on

`bands.py` and `tuner.py` both state, as a fact about the hardware, that shortwave
cannot be swept:

> `rtl_power -D` hardcodes direct sampling mode 1 (the I branch) while this hardware
> wires the Q branch. Fixing it means patching a C tool, so HF listening works and HF
> sweeping does not.

**That is true of `rtl_power` and false of everything else in the package.** Verified
against the Debian trixie source of `rtl-sdr 2.0.2-2`, and re-verified independently:

| tool | flag | what it passes |
|---|---|---|
| `rtl_power` | `-D` | `verbose_direct_sampling(dev, 1)` — I branch, hardcoded ✗ |
| `rtl_sdr` | `-D` | `verbose_direct_sampling(dev, 2)` — Q branch, hardcoded ✓ |
| `rtl_tcp` | `-D` | `verbose_direct_sampling(dev, 2)` ✓ |
| `rtl_fm` | `-E direct2` | 1 or 2, as asked ✓ |
| **SoapySDR** | `writeSetting("direct_samp", "2")` | **any mode, at runtime, on a live stream ✓** |

The mechanism is `librtlsdr.c`: `rtlsdr_demod_write_reg(dev, 0, 0x06, (on > 1) ? 0x90 :
0x80, 1)` — the `> 1` **is** the I/Q ADC swap. There is no C patch and no source build.

**The second half is that `rtl_power` can never be fast, under any configuration.**
`rtl_power.c` parses the interval as an int and clamps `if (interval < 1) interval = 1;`.
One second is a floor in the C. `bands.py:72` has always said what the fast tier needs —

```
LIVE_FAST = "fast"  # one hop; rtl_sdr + FFT; ~10 fps; the radio never looks away
```

— and **29 sections claim it**, while the only thing that reads `live` is `validate()`
(⟲ the first draft said "nothing branches on it"; see §6.7). Every one of those sections
is served today by the same 1 fps `rtl_power` stream as `LIVE_SLOW`. This plan is not
inventing a tier; it is paying a promise the table has been making since it was written.

**And ten of those 29 are HF sections `viewable()` refuses outright.** The table is not
merely optimistic about them — it is currently describing rows that cannot be drawn.

## 2. What we own once we own the samples

1. **~10 fps instead of 1**, on one hop, at 100% duty cycle. The radio never looks away.
2. **Shortwave becomes viewable and surveyable.** Ten sections that today refuse.
3. **Bin width becomes ours.** `rate / N`, with `N` chosen — no negotiation with a tool
   that grants power-of-two divisions of its own choosing.
4. **True RF power in dBFS**, for the first time. Everything the system calls a signal
   level today is `max(abs(sample))/32768` over **demodulated PCM** (`listen.py:378`) —
   post-discriminator audio loudness, not RF. It is why the squelch at `listen.py:211`
   is a hand-tuned fraction and not a dB figure.

## 3. The toolchain: SoapySDR on a `debian:trixie-slim` base

The first draft chose `rtl_sdr -D` piped into numpy because it was the smallest change,
and rejected SoapySDR as "structurally impossible here" — Debian's `python3-soapysdr` is
a CPython **3.13** ABI extension while the image runs CPython **3.12**. ⟲ That is a fact
about `python:3.12-slim`, not about the box, and it evaporates when the base changes.

**Measured in this repo's own environment, not taken on trust.** `debian:trixie-slim`
plus `python3 python3-numpy python3-soapysdr soapysdr-module-rtlsdr rtl-sdr ffmpeg
direwolf`:

```
python 3.13.5
soapy api 0.8.0  abi 0.8        numpy 2.2.4 (Debian's)
modules ('/usr/lib/x86_64-linux-gnu/SoapySDR/modules0.8/librtlsdrSupport.so',)
sidecar imports OK              ← server, listen, packets, usbdev, UNCHANGED
rtl_fm=yes rtl_sdr=yes rtl_power=yes ffmpeg=yes direwolf=yes
	[-D enable direct sampling (default: off)]        rtl-sdr 2.0.2-2+b1
direct_samp -> True   iq_swap -> True   offset_tune -> True
biastee -> True   digital_agc -> True   bufflen -> True   buffers -> True
```

626 MB with apt lists cleaned. The two results worth calling out: **the existing sidecar
imports unchanged on 3.13**, so the rebase costs no application code; and `bufflen` /
`buffers` are settable, so the librtlsdr ring depth is reachable — which the pipe path
cannot do at all.

| option | new build tooling | mode 2 | retune/zoom | verdict |
|---|---|---|---|---|
| **SoapySDR + numpy, trixie-slim** | **none — all apt** | ✅ runtime | **live** | **chosen** |
| `rtl_sdr` pipe + numpy | none, but needs pip | ✅ launch-time only | process restart | dominated |
| `pyrtlsdr` | none | ✅ | live | binds fork-only symbols; see below |
| GNU Radio + gr-soapy | apt | ✅ | retune live, **zoom rebuilds the flowgraph** | +759 MB, 242 pkgs |
| SatDump | apt | n/a | n/a | an app, not a library — but see §7 |
| csdr / pycsdr | source build | ✅ | live | not in Debian; apt repo stops at bookworm |
| SDRangel headless | source build | ✅ | live | real REST API, not in trixie |
| OpenWebRX+ | container | ✅ | live | a whole product — see §7 |
| `soapy_power` | pip | ✅ | n/a | still a sweeper, still text; last release 2019 |

**Why SoapySDR wins, in the order the reasons matter:**

- **Retune and zoom stop blanking.** `rtl_sdr -D` can only be reconfigured by dying, so
  every pan and zoom costs 200–500 ms of blank canvas. `setFrequency` is an I2C write on
  a live stream. On a waterfall that is the whole interaction.
- **Overruns become a return code.** `readStream` returns `SOAPY_SDR_OVERFLOW`. ⟲ The
  first draft prescribed scraping `rtl_sdr`'s stderr for an overrun message; the review
  read all of `rtl_sdr.c` and `librtlsdr.c` and **there is no such message** — when the
  callback blocks, libusb simply stops resubmitting and the RTL2832U's FIFO drops
  silently. The mitigation the first draft called "not optional" was built on a signal
  that does not exist.
- **HF and VHF in one uninterrupted session**, because `direct_samp` is a register write.
- **The achieved sample rate is readable.** librtlsdr quantises to the 28.8 MHz divider,
  so requested ≠ actual; `bin_hz = rate/N` is only exact if you know which. `rtl_sdr`
  never tells you.
- **It restores the Dockerfile's rule instead of bending it.** Everything is apt, so
  "No pip install at all" survives intact — the first draft argued for weakening it.
- **Hardware portability is real in Debian today**: `soapysdr-module-airspy`, `-hackrf`,
  `-bladerf`, `-lms7`, `-uhd`, `-remote`. An Airspy HF+ — real gain control and real
  filtering where the RTL2832U has ~7 effective bits and none — becomes a config string.

**Why not `pyrtlsdr`:** 0.4.0 and 0.5.0 bind `rtlsdr_set_dithering` and seven
`rtlsdr_*_gpio_*` symbols **unconditionally**; those exist in the rtl-sdr-blog fork and
not in osmocom librtlsdr, so `import rtlsdr` raises `undefined symbol:
rtlsdr_set_dithering`. The escapes are a 2023 pin, or `pyrtlsdr[lib]`, which ships a
*second forked librtlsdr* plus its own libusb — two drivers in one image disagreeing
about device enumeration, on a two-dongle box.

**Why not GNU Radio:** +759 MB and 242 packages, pulling `python3-pyqt5` **and** `-pyqt6`
and `gnome-terminal | x-terminal-emulator` — a radio sidecar on a headless box depending
on a terminal emulator is the tell. It still rebuilds the flowgraph to zoom, and the
things it saves later are 40–150 lines of scipy each.

### FFT cost

Measured, 4096-bin Welch over **every** non-overlapping segment of a 100 ms frame of
2.4 MS/s: **8.5 ms**, plus 1.2 ms for the u8 → complex64 conversion — ~9.7 ms/frame,
~10% of one core at 10 fps, on a 2.80 GHz Xeon (the owner's box is faster; the direction
is safe, the absolute figures are not theirs). So take the maximum-quality configuration:
there is no reason to sample a subset of segments and reintroduce the looking-away
problem the fast tier exists to remove. `np.fft` releases the GIL, and SoapySDR's SWIG
module is built with `-threads`, so `readStream` releases it too — which is what lets
this coexist with `http.server` in one process.

## 4. Shortwave, honestly

**The samples are still complex.** `rtlsdr_set_direct_sampling` writes `0xb1 = 0x1a` —
"disable Zero-IF mode" **turns the RTL2832U's digital downconverter on** — and
`rtlsdr_set_center_freq` routes to `rtlsdr_set_if_freq`, writing into the DDC's NCO. What
arrives is ordinary complex I/Q. **A plain complex FFT. No `rfft`, no folded
half-spectrum, no special case in `iq.py`.**

**But the underlying stream is real, so a capture is only honest inside a window:**

> A capture at centre `fc`, rate `R`, is non-redundant iff **`R/2 ≤ fc ≤ 14.4 MHz − R/2`**.

Legal rates are **225001–300000 Hz** or **900001–3200000 Hz** (`rtlsdr_set_sample_rate`
rejects the rest, identically in direct sampling). At 2.4 MS/s the honest window is
1.2–13.2 MHz, and exactly two of the ten HF sections fall outside it — the review checked
all ten and found no others:

- **`mw`** (0.53–1.70, centre 1.115): passband −0.085…+2.315 MHz. Everything below 0 Hz
  is a mirror of just above it, and the ADC's DC offset lands in-band under the same
  condition. **1.5 MS/s** rather than the first draft's 1.2 — ⟲ 1.2 fills 97.5% of its
  own passband, and this project's own honesty standard elsewhere is `LIVE_FAST_MAX_HZ`,
  which trusts only 83%. 1.5 still satisfies `R/2 ≤ fc` and leaves real margin.
- **`20m`** (14.15–14.35, centre 14.25): runs to 15.45 MHz, past Nyquist, folding
  14.4–15.45 onto 13.35–14.4 *within the same picture*. **250 kS/s** → 14.125–14.375.
  ⟲ At 250 kS/s, 4096 bins is 61 Hz — below `MIN_SWEEP_BIN_HZ = 100`, so the headline
  "4096 bins" is unreachable for this one section. Bin count must be derived from the
  rate, not fixed, and `validate()` should say so rather than let it 502 at runtime.

⟲ **Every HF section needs a rate, not just those two.** With one global 2.4 MS/s,
`sw-41m` (0.25 MHz wide) draws 6.125–8.525 MHz while the band button still says
"7.200–7.450" — the label and the axis disagree and nothing notices.

**`mirrored` is both dead and mis-stated.** Every section stops at ≤14.35 MHz, so the
flag is `False` for all ten — while *all ten* carry a reversed image from `28.8 MHz − f`:

| section | image from (reversed) | notable content |
|---|---|---|
| `mw` 0.53–1.70 | 27.10–28.27 | CB 26.965–27.405 → lands at 1.395–**1.700** (⟲ clipped by the section edge, not the whole CB band) |
| `40m` 7.125–7.30 | 21.50–21.675 | 13 m broadcast |
| `sw-41m` 7.20–7.45 | 21.35–21.60 | 15 m amateur + 13 m broadcast |
| `20m` 14.15–14.35 | 14.45–14.65 | quiet, but immediately adjacent |

And the out-of-HF breakthrough nothing in the repo mentions: **FM broadcast 88–108 MHz
folds onto 1.6–14.4 MHz** — a local at 96.0 lands at 9.6 MHz, mid-31 m band. ⟲ Airband
118–137 folds to **2.8–14.4 MHz**, not 2.8–21.8: anything above 14.4 reflects back down.
The board's LF/MF/HF diplexer attenuates the VHF folds by an unpublished amount and gives
**zero** protection against the 14.4–28.8 MHz images, which sit inside its passband. None
of it is removable in software — the two contributions are summed in one bin.

So the honest UI is per-section *"carries a reversed image of X–Y MHz"* plus one global
caveat, derived as `image_start_hz`/`image_stop_hz`, replacing `mirrored`.

**Gain really is dead below 24 MHz.** `rtlsdr_set_direct_sampling` calls
`dev->tuner->exit(dev)`, and the 0–49.6 dB is *tuner* gain. With ~7 effective ADC bits
plus ~36 dB of processing gain at N=4096 (⟲ 36.1, less ~1.8 for a Hann window — the first
draft said 33) there is enough range for a useful waterfall, but a strong MW carrier can
clip the ADC and there is nothing in software to do about it.

⟲ **The inversion question is settled and does not gate anything.** Working the NCO
arithmetic — `if_freq = ((freq × 2²²)/rtl_xtal) × −1`, truncated to 22 bits — for
`fc = 20.5 MHz` gives a mixer at **+8.3 MHz** (= 28.8 − 20.5), so a tone at 20.5 + δ
aliases to −8.3 + δ and mixes up to +δ: **upright**, in every zone, because the modulo
wrap and the aliasing are the same arithmetic. The classic "even zones invert" rule is
about real one-sided downconversion, not a complex DDC. The first draft called this an
open disagreement and blocked F1 and F2 behind measuring it; no shipped section even
reaches above 14.35 MHz, so it only affects the wording of the image caveat.

## 5. What 10 fps actually breaks

The first draft said the `Frame` contract is unchanged, therefore "the SSE stream,
`sdrSpectrum.ts` and the canvas are untouched — the frontend is already done." ⟲ The
*schema* is unchanged. Everything downstream sized for 1 fps is not, and this was the
single biggest hole in it.

**The canvas repaints its whole history per row.** `SdrWaterfall.tsx:108` calls `blit()`
inside the per-row subscription, and `paint()` allocates a fresh `bins × 180 × 4` array
and runs `shade()` per pixel. At 4096 bins that is **737,280 `shade()` calls and 2.95 MB
allocated per frame** — at 10 fps, 7.4 M calls/sec and 29.5 MB/s of garbage on a phone's
main thread.

**`ROWS = 180`** is documented as "three minutes at one row a second" → **18 seconds**.
**`CALIBRATION_ROWS = 8`**, documented "over in eight seconds" → **0.8 s**, well inside
the settling window after a retune: freeze a bad scale there and the whole session is
painted wrong, silently, which is exactly what that file exists to prevent.

**The wire.** A 4096-bin frame serialises to **28,750 bytes**; at 10 fps that is
**2.30 Mbit/s per viewer** (today: 28 KB/s) over a link the owner reaches **remotely**,
plus 2.69 ms/frame of GIL-held `json.dumps`. `SPECTRUM_QUEUE = 4` is commented "two
seconds' worth" → **0.4 s**, and `_publish_frame` drops the **arriving** frame when full,
so ordinary WAN jitter starts punching holes in the time base. And `api/sdr.py:947`
relays with `async for line in upstream.aiter_lines()` on the **main API event loop**.

None of that is optional work, and none of it is fixed by changing the sample source.

## 6. Corrections carried from the independent review

1. **CI.** `supervisor/tests/test_sdr_listen.py` loads `deploy/sdr/listen.py` by path **at
   module scope**, and `supervisor/uv.lock` has no numpy. A top-level `import numpy` — or
   `import SoapySDR`, which is apt-only and absent from the dev env — fails collection of
   the whole file. Hence `radio.py` and `iq.py` as separate modules (§7 F2/F3).
2. **Two sidecar guards refuse HF independently of the backend**: `listen.py:408`
   `Sweep.of` and `server.py:131` `_range_of`. Relaxing only `tuner.py` and `sdrBands.ts`
   turns ten disabled buttons into 502s.
3. **`sweepable()` is shared with the debug `/sdr/sweep` route**, which still drives
   `rtl_power`. Relaxing it flat re-opens HF sweeps that genuinely cannot work; it needs
   to become two predicates, one per engine.
4. **The bin-width claim was never implemented by any wave.** `live_bin_hz`, `_span`'s
   `chosen_bin`, the `[100, 100_000]` query bounds and the `SPECTRUM_MAX_BINS` check all
   survive untouched. Either a wave changes them or §2.3 is a lie.
5. **`stdbuf -oL` must not survive the engine swap** — on binary output it means a flush
   per `0x0A` byte. (Moot under Soapy, which has no pipe at all; noted so it is deleted
   rather than inherited.)
6. **`Frame.db` from numpy will not serialise.** `round(np.float32)` returns `np.float32`,
   which `json.dumps` rejects; `iq.py` must `.tolist()`.
7. **`validate()` does branch on `live`, twice** — and one rule ("an HF section may not be
   `slow`") is load-bearing for exactly this change, so redefining `surveyable` silently
   changes what it asserts.
8. **`sameBand` is fragile to a jittering `start_hz`.** `sdrSpectrum.ts:89` resets the
   history *and the frozen colour scale* whenever `startHz`/`binHz`/`db.length` changes.
   ⚠ This gets **more** dangerous under Soapy, not less: reading back the *achieved*
   sample rate is a feature, and a ±1 Hz flap in a derived `start_hz` would re-blank the
   waterfall every frame. `start_hz` must be computed from the requested integers.
9. **The survey path is safe, and the plan must say so.** `sweep.py` is entirely relative
   — per-bin percentile floors, `p20/p99.5` for the ramp, the same figures
   `sdrWaterfall.ts` uses — so live dBFS alongside `rtl_power`'s dB breaks neither the
   reducer nor the shared colour scale. But F8 would otherwise put two different
   quantities called "dB" in one UI with nothing distinguishing them.
10. **`_drain_tuner_log` prints `[rtl_fm]`** whatever tool ran.
11. **The Dockerfile rule is duplicated** verbatim in `listen.py:752`. Rule 9 means both.
12. ⚠ **A leaked Soapy device handle is the new orphan.** `1a64ad0` fixed a `_restart`
    race that stranded an `rtl_fm` holding the dongle with nothing pointing at it. Moving
    spectrum in-process removes the child process for that path — and replaces it with a
    stream and a device handle that must be closed on exactly the same paths. `radio.py`
    owns that, and the survivor registry has no visibility into it.

## 7. Waves

**F0 — hardware truths. Blocks only F7.** ⟲ Demoted from a global gate. With a dongle on
the bus: Soapy enumerates it and `serial=` filtering selects the right one; `direct_samp=2`
returns Q-branch samples; **retune and sample-rate change on a live stream** (the claim
the whole ranking rests on and the one thing packaging cannot prove); `SOAPY_SDR_OVERFLOW`
is actually reported under induced backpressure; and WWV at 5/10/15 MHz for the image
caveat's wording. F1–F6 need no radio.

**F1 — the base rebase.** `debian:trixie-slim`; `python3 python3-numpy python3-soapysdr
soapysdr-module-rtlsdr` beside the existing `rtl-sdr ffmpeg direwolf`; `python` → `python3`
in `CMD` and `HEALTHCHECK`; `python3 -c "import server, SoapySDR, numpy"` added to the
build gate beside the `command -v` checks. No pip, so the Dockerfile's rule is restored
rather than bent — and both copies of it are corrected. `numpy` into
`supervisor/pyproject.toml`'s dev extra + regenerated `uv.lock` + `scripts/dev-setup.sh`.

**F2 — `deploy/sdr/iq.py`: samples → frames.** Pure numpy, no radio, no subprocess: LUT →
`complex64`, segment, window, batched FFT, Welch, dBFS, `.tolist()`. Tests drive synthetic
tones — a tone at a known offset lands in a known bin at a known level.

**F3 — `deploy/sdr/radio.py`: the device behind a protocol.** `import SoapySDR` lives here
and nowhere else, so the by-path tests keep working against a fake and the image's build
gate proves the real one. Owns open/close, `setSampleRate` **and reading back the achieved
rate**, `direct_samp` below 24 MHz, `setFrequency`, `setupStream`/`readStream` into a
preallocated buffer, and the overflow return. Teardown is this module's whole
responsibility (§6.12).

**F4 — the `spectrum` purpose switches engine.** `Frame` unchanged. **The wire budget is
decided here**, not discovered later: cap live bins, or send `db` as a compact binary
form, or negotiate frame rate — and re-derive `SPECTRUM_QUEUE` from seconds rather than
rows. Overflow surfaces as a stream condition the way `dutyNote` surfaces hop cost.

**F5 — the PWA survives 10 fps.** Decouple paint from arrival (rAF-coalesced, or
scroll-and-append into an offscreen canvas); re-derive `ROWS` and `CALIBRATION_ROWS` from
*seconds* and the live frame rate; fix `start_hz` to the requested integers. New wave,
entirely from the review.

**F6 — the band table learns the truth.** Per-section `sample_rate_hz` with the Nyquist
window and the bin-floor enforced in `validate()`; `surveyable` split into per-engine
predicates; `mirrored` → `image_start_hz`/`image_stop_hz`; `LIVE_FAST_MAX_HZ`'s rationale
noted as not applying below 24 MHz, with an HF equivalent rather than nothing.

**F7 — HF goes live.** All four refusals: `tuner.sweepable`, `tuner.viewable`,
`listen.Sweep.of`, `server._range_of` — per purpose, so the `rtl_power` survey keeps
refusing. `sdrBands.ts:whyNotLive` stops saying "shortwave cannot be swept", and the image
caveat and "no gain control here" appear where they will be read.

**F8 — stop calling loudness a signal level.** The spectrum path reports true dBFS; the
listening path keeps `rtl_fm`, so its `peak` stays audio loudness and gets **labelled** as
such. The two dB quantities are distinguished in the UI (§6.9).

## 8. Deliberately not in this plan

**Replacing `rtl_fm` with a numpy demodulator.** Affordable — NFM demod is ~1.5 ms per
100 ms of I/Q — and it would give one radio spectrum *and* audio with one dB scale
everywhere. The blast radius is why not: that PCM is the substrate for `_peak`, the
segment cutter, whisper captions, the direwolf feed and the recordings library. Six
features to re-validate for simultaneity that **two dongles already buy**, since the lease
is per-radio. SoapySDR does not foreclose it, which is part of why it is the right base.

**A real scanner** — cheap once `iq.py` exists, and a follow-on rather than a hidden extra.

**The multi-job-per-radio question.** It breaks the "one radio, one job" assumption
`roles.py` and the sidecar's 409 are built on, and deserves a deliberate decision.

**OpenWebRX+ on a second dongle.** It already does waterfall + multi-VFO + HF + a decoder
zoo (AIS, SSTV, FLEX, POCSAG, HFDL, VDL2, ACARS, RDS, WSPR/FT8, and DMR/D-Star via
softmbe), and `luarvique`'s fork is actively released where upstream has stalled. It is
**not** a replacement for this waterfall: it holds the dongle for its uptime, its
WebSocket is an internal unversioned protocol rather than an API, and adopting it would
cost the omnibox, the level meter, the caption path, `bands.py`'s measured duty-cycle
honesty and the debug-console operability CLAUDE.md #10 protects. As an independent
container on radio B, linked rather than embedded, it costs nothing and buys all of it.

**SatDump as a scheduled `PURPOSE_SATELLITE` lease.** `satdump 1.2.2-1` is in trixie and
decodes NOAA APT, Meteor LRPT and GOES HRIT. It is an application, not a library — which
suits it perfectly to the Phase 5 scheduler taking a lease for a predicted pass, writing
images through the storage abstraction, and releasing. No new DSP, no new stack.

**A pre-existing bug this makes visible and does not fix:** `listen.py` applies `-E
direct2` to everything below `MIN_HZ` (24 MHz), so 14.4–24 MHz tunes into the second
Nyquist zone. No table row lives there, but `defaults_for()` lets the owner type
20.000 MHz by hand and get an unlabelled aliased capture with an image from 8.8 MHz.
F6's window rule is what keeps the table honest; the expert path needs the same check.
