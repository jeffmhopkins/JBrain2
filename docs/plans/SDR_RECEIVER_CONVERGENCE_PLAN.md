# SDR receiver convergence — one capture, many sinks, and the subprocesses go

> **Status:** In progress · **Last verified:** 2026-09-06 · **Waves:** W1✅ W2✅ W3✅ W4✅ W5◻️ W6◻️ W7◻️

> Reconciled with the root `CLAUDE.md` non-negotiables: no LLM call is added (rule 1);
> nothing new is written to disk — W5 *removes* a temp-file path (rule 2); no new table,
> so no new RLS surface (rule 3); every operator control stays a PWA or debug-console
> surface (rule 10). Rule 11 shapes every wave: `deploy/sdr/` is linted and typechecked
> by nothing and tested by `supervisor`'s pytest, so all of it is verified from
> `supervisor/`. No new runtime dependency is proposed — see **Rejected** below.

Four independent reviews (2026-09-06) read `deploy/sdr/` against SoapySDR's API and
source, SoapyRTLSDR, librtlsdr, gnuradio, gqrx, SDR++, OpenWebRX, csdr and liquid-dsp.
The question put to them was the owner's: *"we shouldn't be recreating paradigms so
much."*

**The answer is narrower and more useful than "yes".** The DSP layer is right and
conventional — `demod.py` and `iq.py` are pure block chains with no device in them,
which is how gqrx and GNU Radio factor it, and three things that look like
reinvention turn out to be forced by this hardware. **One paradigm really was
invented, one layer up: `purpose` is a SINK TYPE wearing a LEASE TYPE's clothes.**
Everything else on this list is a defect or a deletion.

This plan is the inventory and the order. Findings marked **[V]** were reproduced
directly against the code or the box; **[R]** is a reviewer's measurement not
independently re-run; **[S]** is suspected and needs hardware to settle.

## A — Paradigm changes

| # | What we do | What every SDR application does | Consequence |
|---|---|---|---|
| **A1** | Four mutually-exclusive session `purpose`s per radio (`listen`/`spectrum`/`survey`/`aprs`), each dispatching to one terminal pipeline (`listen.py:1078`). Consumers then guess which lease serves them — `for_purpose`, `drawing`, `_worth_showing`. | One device reader, fanned out to many sinks: waterfall FFT, one or more channel demodulators, decoders, recorders. SDR++ VFOs, gqrx's receiver chain + FFT tap, OpenWebRX's per-client chains, GNU Radio's explicit fanout. | Audio and a picture of the band are mutually exclusive *by construction*, on a radio that is already capturing both. The fanout **already exists, proven**, in `_pump_iq_listen` — one `read`, two sinks — hardcoded for exactly one pair. |
| **A2** | `tune()` tears the pipeline down and rebuilds it (`_restart`, `listen.py:2206`), justified by "`rtl_fm` cannot be retuned in place" (`listen.py:31`). | Retune is a source parameter. Nobody destroys a stream to change frequency. | The justification is obsolete on the primary engine: `Radio.retune` exists and `_sweep_hops` retunes a **live** stream eleven times a second. Every subtle guard in the file — `_restarting`, the `alive` special case, the second `_released` re-check — is a consequence of a choice that no longer applies, and each documents a real measured incident. The user hears a gap on every retune. |
| **A3** | A listening session captures 2.4 MHz and transforms only the ±16 kHz channel (`listen.py:1361`). | The default screen: full-span waterfall **and** demodulated audio from one stream. | "Listen or look at the band, pick one" is invented, not physical, for any span ≤ 2.4 MHz. ~~+2.8% of one core~~ — **that figure was wrong and W3 re-measured it: 11.4%** (9.3 ms of transform + 2.1 ms of `peaks.find` per 100 ms frame, 4096 bins, Welch at the 50% overlap W2 added). Fewer bins costs MORE, not less — 1024 bins is 13.9% — because Welch averages every segment that fits. Affordable because the sink transforms nothing while nobody is subscribed to the band. |
| **A4** | One demodulator per session. | Multiple VFOs off one capture. | Not proposed now. Falls out of A1 for free — a second `Demodulator` at a different `offset_hz` off the same `Reading` costs no extra USB bandwidth. Listed so A1 is designed to allow it. |
| **A5** | `survey` is a fourth session kind with its own lifecycle, its own temp CSV, and a synchronous handler that pins a thread for up to 900 s. | A survey is an accumulator over spectrum rows. | Strictly *less* capable than `spectrum`: it refuses shortwave, because `rtl_power` hardcodes direct-sampling mode 1 and this board wires the other branch. |

## B — Home-rolled things to delete

**We should not be timid here.** Each row says what replaces it.

| # | Delete | Replace with | Why it goes |
|---|---|---|---|
| **B1** | The `rtl_power` **spectrum** fallback (`_spectrum_cmd`, `_pump_spectrum`, `Stitch`, ~180 lines) | An honest refusal naming the driver | **[V]** Both engines land on the same `Frame.db`, the same colour map, and the same `peaks.find` — **whose output reaches the agent's tools as measurement**. `iq.py` emits true dBFS; `rtl_power` emits its own uncalibrated scale. A silent engine swap that changes what a number *means*, feeding an LLM that reads it as fact, is a correctness bug wearing a robustness costume. |
| **B2** | `PURPOSE_SURVEY`, `_sweep_cmd`, `_start_sweep_pipeline`, `sweep_csv`, its lifecycle rule | A `SurveySink` over a `spectrum` session, emitting the CSV shape `backend/src/jbrain/sdr/sweep.py` already parses | Removes a purpose, a lifecycle, a temp file, and gains shortwave surveys. Its only caller is one debug route. |
| **B3 ✅** | `peaks._median` + `_local_floors` — a pure-Python `sorted()` per bin | `np.lib.stride_tricks.sliding_window_view` + `np.partition` on a stride, `np.interp` back | **[V]** 238–1910 ms against a 100 ms budget, on the capture thread. Vectorised: **0.88 ms**. |
| **B4** | The gap-based fold at `peaks.py:118` | A minimum peak-to-peak distance (`0.6 × channel_hz`, the number the client already uses) | **[R]** Two stations one raster apart always have a clear gap smaller than the raster, so they always merge. Measured: two FM stations 200 kHz apart → 1 signal. |
| **B5** | `_Fir.delay` (`demod.py:297`) | — | No caller anywhere in `deploy/`. |
| **B6 ✅** | `server.py`'s duplicate `MODES`, `MIN_HZ`, `MAX_HZ`, and `WBFM_SAMPLE_RATE = 171_000` | Import from `listen` | **[V]** The constant is dead *and* contradicts `listen.py:344`'s measured 192_000, which carries two paragraphs explaining why 171 kHz was wrong. |
| **B7** | `_SHOWN_FIRST` / `_worth_showing` (`listen.py:2509`) | The same policy in `backend/.../sdr.py`, which already reshapes `SessionInfo` | Presentation policy in the radio process. Mostly stops being a question after A1. |

**Kept deliberately — do not delete:**
`rtl_fm` under `listen` (a genuine degraded mode, visible via `SessionInfo.engine`, and CLAUDE.md #10 says an owner with no terminal must not lose audio to a driver regression); **direwolf** (a decoder, not a driver — bit sync, NRZI, HDLC, CRC; reimplementing it *would* be the reinvention); `http.server` and hand-rolled routing (correct under apt-only); `_park`/`reap_survivors` (a process wedged in a USB ioctl does not die for SIGKILL either); the per-radio lease that refuses rather than queues; self-describing `Frame`s; MP3 for late joiners.

## C — Defects

### C1 tier — stop for these

| # | Finding | Evidence |
|---|---|---|
| **C1 ✅** | **Use-after-free.** `read_into` calls `readStream` outside `self._lock`; `close()` nulls the handles under it and frees them outside. `_kill` (`listen.py:2142`) closes the radio **without joining the pump threads**; `stop()` never joins. A pump inside `readStream` while `closeStream` frees the buffer vector and `unmake` deletes the device is a segfault of the container. `held.alive` is TOCTOU. | **[V]** by reading both call paths |
| **C2 ✅** | **`_build_back` places its cutoff at the passband edge** — the identical bug fixed in `_build_front` on 2026-09-06, left in the sibling function. `stop` is computed for the tap count and discarded. | **[V]** measured through nfm: −3.1 dB @ 2 kHz, **−6.6 @ 3 kHz**, −12.5 @ 4 kHz. Muffled consonants on every NFM/AM voice signal. AM has no de-emphasis alibi. |
| **C3 ✅** | **`peaks.find` blows the frame budget 2–19×** on the capture thread, so the wideband waterfall is throttled and USB buffers overflow — defeating the "the radio never looks away" property `iq.py` exists to guarantee. | **[V]** 238 / 321 / 1910 ms vs 100 ms. Retroactively explains the "0.33 fps on the FM dial" the runbook blamed on retune settle. |

### C2 tier — measured, fix soon

| # | Finding | Evidence |
|---|---|---|
| **C4 ✅** | **`LISTEN_OFFSET_HZ = 240_000` folds the LO/DC spike onto the tuned station.** 240 kHz is exactly 5 × the 48 kHz IF, so the receiver's own DC offset aliases to **0 Hz**, suppressed only by the window's stopband. Shipped as **126_000** (2.4 MS/s ÷ 19), not the 300 kHz first proposed: 300 kHz fixes the narrow modes but still lands the spike inside wide FM's channel, and every divisor from 5 to 25 was measured to find the one whose worst case across nfm/wbfm/am is lowest. | **[V]** residue at **+0 Hz, −65 dB** before; **−157 dB at +17.7 kHz** after. |
| **C5 ✅** | **`_build_channel` builds a 53-tap wide-FM filter its own docstring says it returns `None` for.** Introduced 2026-09-06 when the guard moved from `view_half_hz` to `0.45 × if_rate`. Costs 18% of the wide-FM chain and narrows the signal. | **[V]** |
| **C6 ✅** | **The detection baseline window can only grow and is never clamped to the row.** `max(400 kHz, 21 × channel_hz)` exceeds the whole row for any sweep < 400 kHz, every 256 kS/s capture, and any 200 kHz raster — silently degrading to a global median. This is the 162.55 blind spot generalised, and a test currently *pins* the behaviour. | **[V]** the constants, and the on-air miss |
| **C7 ✅** | **`SessionInfo.engine` is never set to `"rtl_power"` and never set in `_start_iq_spectrum`.** A waterfall on our own engine reports `"rtl_fm"`. `server.py:1175` works around it by reading a private attribute with a `noqa`. | **[V]** |
| **C8** | `Device.unmake(device)` bypasses the SWIG binding's own deleter, so `__del__` unmakes a second time and throws inside the destructor on every teardown. | **[R]** binding source read |
| **C9** | `QUEUE_BUFFERS = 4` is a spectrum-path tuning applied to the listening path: 41 ms of ring at 2.4 MS/s, and SoapyRTLSDR discards the **entire** fifo on one overflow event. Any ffmpeg stall over 41 ms tears audio. A listening session never retunes mid-session, so the shallow ring buys nothing there. | **[R]** |
| **C10 ✅** | The DC/LO bin is excised in the *probe* (`radio.py:1000`) and nowhere in production. On a stitched hop row that is a comb of up to 11 phantom stations, and `steady` is precisely the classifier that cannot absorb it. | **[R]** |
| **C11 ✅** | Welch segments do not overlap; the textbook and `scipy.signal.welch`'s default is 50%. On the tuning row: per-bin σ **1.49 → 1.09 dB**, and an empty channel's apparent SNR falls from a mean of 3.89 dB to 3.08 — against a 6 dB threshold with only ~0.7 dB of headroom today. | **[R]** |

### C3 tier — real, lower urgency

| # | Finding | Evidence |
|---|---|---|
| **C12** | Every filter is Hamming, so alias rejection is a property of the window (~−53 dB) rather than a specification. Worst measured leakage into the demodulated channel: **−56 dB**. A local blowtorch 60–70 dB over a weak station is audible in it. Fix: `np.kaiser` with taps and β from a target attenuation, and change `lowpass`'s signature to `(pass_hz, stop_hz, atten_db, rate)` so the "is cutoff the edge or the 6 dB point?" question — which has now produced **two** separate bugs — becomes unaskable. | **[R]** |
| **C13** | No AGC on AM/SSB. Same RF level: nfm −11.4 dBFS, usb −23.0, am −31.0 — a 20 dB swing on mode change, and a weak AM/SSB station is simply inaudible. `rtl_fm` behaves the same, so it is parity; every listening application runs AGC here. | **[R]** |
| **C14** | SSB is modelled with a symmetric `channel_half_hz`, but SSB is one-sided: the strip shades ±3.4 kHz while the demodulator hears +300…+3400 only. A user centring a signal in the shaded box puts half of it in the rejected sideband. | **[R]** |
| **C15** | `_DcBlock`'s real −3 dB corner is ~7 Hz, not the 31 Hz documented (31.25 is the boxcar's first *null*, where the response is 0 dB), and it overshoots +2 dB at 20 Hz. | **[R]** |
| **C16** | De-emphasis is convolved in at the **IF** rate, making wide FM's back end 771 taps where the anti-alias filter alone is 481. `gr-analog` runs it at the audio rate. 12.3 → 8.0 Mmac/s. | **[R]** |
| **C17** | `readStream`'s `flags` and `timeNs` are discarded, so `Reading.torn` can say *something* was lost but never *how much* — the one number a waterfall needs to place a row honestly. Frame time is wall-clock captured before the read. | **[R]** |
| **C18** | `_settle_fixed_gain` uses `or`, so a measured gain of exactly **0.0 dB** — the value this box actually had — is treated as absent and replaced by 30. The "fixed gain" comparison was against a different gain. | **[R]** |
| **C19** | `radio._claim` uses exact-match keying where `listen.blocking_key` has the correct rule (an unnamed holder blocks everything). Guarded one layer up today, so latent. | **[R]** |
| **C20** | Half-bin convention conflict: `peaks.py` treats `start_hz + i·bin_hz` as the bin **centre** (correct, verified); `sdrTuning.ts` and `server.py:222` add a further half bin. The box's peak frequencies and the tuning readout are on two different grids. | **[R]** |
| **C21** | `_channel_floor`'s outer ring for wide FM is 90–180 kHz off centre — where the 200 kHz raster puts the neighbour's sideband. On a crowded dial the "noise floor" is a neighbouring station. | **[S]** |
| **C22** | Neither `Frame` nor `Reduced` carries `bin_hz`-relative floor semantics or `gain_db`, so a floor from an older run is silently incomparable — the same class as the AGC bug just fixed. Thresholds calibrated at one resolution do not transfer to another. | **[R]** |
| **C23** | `Frame.as_dict` does `[round(v,1) for v in self.db]` per subscriber per frame — the exact per-row cost `iq.py` says it eliminated with `np.round`, still paid on this path. | **[R]** |
| **C24** | Any stage with `m == 1` raises from the constructor (cutoff lands exactly on Nyquist). Unreachable from `listen.py` today; a trap for any new capture rate. | **[R]** reproducible |
| **C25** | No ppm/`CORR` correction anywhere. Low on a TCXO dongle (~80 Hz at 162 MHz), but two dongles will differ from each other. | **[R]** |
| **C26** | Gain is written on the direct-sampling path, where the tuner is bypassed and the number is fiction. | **[R]** |
| **C27** | `setBandwidth` is never called; librtlsdr's automatic IF bandwidth is exactly the rolloff `hop_usable_bins` throws away a sixth of every capture to avoid. Setting it explicitly might buy much of that back. | **[S]** probe rung, not a blind change |
| **C28** | `hop_usable_bins` is `bins * 5 // 6` in `listen.py` and `TRUSTED_FILL` in `bands.py`. They agree today; changing the constant desynchronises the planner from the stitcher silently, and the stitched row's bin→Hz mapping is then wrong with nothing to detect it. | **[R]** |

## D — Verified correct: do not churn

Recorded so no future wave "improves" a thing that was measured right.

- **FM discriminator and deviation→full-scale scaling.** Tracks prediction to <0.3% across dev 500 Hz–75 kHz; harmonics ≥76 dB down. `x[n]·conj(x[n−1])` is `gr::analog::quadrature_demod`'s form.
- **`_split`'s coarse-first ordering.** Every factorisation of 50 and 10 enumerated and costed; the shipped split is optimal in both.
- **`_Fir` state.** Phase and tail arithmetic correct for arbitrary buffer lengths; reversed-tap dot product correct for the asymmetric complex SSB kernel.
- **The midpoint cutoff in `_build_front`.** Reduces to the output stage's Nyquist with taps from the transition width — textbook. C2 is its sibling, left behind.
- **SSB as a Weaver complex bandpass.** 60–90 dB opposite-sideband rejection, −6 dB at 300/3400 (the conventional spec), `2·real()` amplitude correct.
- **De-emphasis convolved into the anti-alias FIR.** Matches the ideal one-pole to 0.1 dB through 4 kHz. Only the *rate* it runs at is suboptimal (C16).
- **`iq.py`'s window and calibration.** Periodic Hann; neighbours at exactly −6.021 dB; a full-scale tone reads **0.0000 dBFS**; no off-by-half-bin at the transform; partial trailing segment correctly dropped.
- **Hand-rolled offset tuning.** `rtlsdr_set_offset_tuning` returns −2 for the R820T2 — `offset_tune` is E4000-only, and SoapyRTLSDR does not check the return, so enabling it would silently do nothing.
- **CF32 over CS8/CS16.** The driver converts through a 64K LUT in C++; taking CS8 and converting in numpy is strictly slower. Upstream's CS16 swap path is built from uninitialised values.
- **`setGain(dir, chan, value)` — the overall overload.** This driver lists one element, `"TUNER"`. There are no `LNA`/`MIX`/`VGA` elements; per-element gain is not an option here.
- **`setGainMode(False)` before `setGain`; the named `setFrequency(…,"RF",…)` overload** — the latter avoids a genuinely dangerous auto-`CORR` path, not merely a wasteful one.
- **`bufflen` validation, partial-read assembly, `SOAPY_SDR_OVERFLOW`/`TIMEOUT` as non-fatal, teardown order.**

## Rejected

- **scipy.** ~60 lines of non-hot-path design code for +63 MB and a second numeric stack in an apt-only image. It cannot replace the three things that matter: no streaming decimating FIR (`resample_poly`/`decimate`/`upfirdn` are one-shot and zero-pad, reintroducing the boundary click the tail exists to prevent), no phase-continuous NCO, and `welch` cannot produce this dBFS contract without more post-processing than it saves. **Steal the two ideas instead** (C11, C12).
- **liquid-dsp.** `libliquid` is packaged; no Python binding is. Using it means hand-written ctypes around the most safety-critical code here — a *larger* hand-roll than what exists.
- **csdr / GNU Radio.** Not in Debian; +759 MB respectively. Cited as references, not dependencies.

## The waves

Each is one PR, per `PROCESS.md`. Verified from `supervisor/`.

**W1 — Stop the bleeding. ✅ shipped 2026-09-06.** C1 (join pumps before close, or an `_io_lock` spanning `read_into` and `close`), C2, C5, C4. Regression tests: a close racing an in-flight read; audio flatness through the back end (there is no such test today, which is why C2 was invisible); DC-spike suppression (also absent).

**W2 — The measuring path tells the truth. ✅ shipped 2026-09-06.** B3 + C3 (vectorised baseline), C6 (clamp the window to the row; move the baseline statistic off the median so a window that is majority-signal still reads noise), C10, C11, C7, B6. Delete the test that pins C6.

**W3 — One capture, many sinks. ✅ shipped 2026-09-06.** A1 as a pure refactor first: lift `_pump_iq_listen`'s body into `Capture` + `Sink.feed`, same two sinks, no behaviour change. Then A3: `Frame.view` ("band"|"channel"), `/listen/spectrum?view=`, and a band sink on the listening session. C9 rides along.

## W3 — what shipped, and what it measured (2026-09-06)

**`deploy/sdr/capture.py` is the new seam**, and nothing in it knows what a session is:
`Sink` (a `want` and a `feed`), `Capture` (one `read`, offered to every sink in turn),
`ChannelSink` (a VFO: demodulator, its audio, and the picture of the channel it hears)
and `BandSink` (the whole capture, transformed). `_pump_iq_listen` and `_stare` are now
the SAME loop with different sinks — which is the finding A1 named: the only difference
between a waterfall and a receiver drawing its band was which pipeline the `purpose`
dispatched to.

- **`Reading` carries its own `center_hz`, and it is required.** A sink that asked the
  RADIO where it is labels a row with wherever the radio went next, which on a hopping
  stream is eleven wrong answers a second. Required rather than defaulted because the
  two test fakes that constructed a `Reading` without one immediately proved the trap:
  both drew rows centred on DC and both had a `center_hz` attribute sitting unused.
- **A listening session draws the band it is sitting in**, off the same buffer as the
  audio, and `Frame.view` (`"band"` | `"channel"`) says which picture a row is. Before
  this, readers inferred it from `passband_hz` being nonzero — which worked only while
  one session could publish one kind, and is exactly the coupling A1 removes.
- **The cost, re-measured, is 11.4% of one core, not 2.8%** (see A3). So `BandSink`
  takes an `active` predicate and transforms nothing while nobody is subscribed to the
  band: a listening session pays for the second picture only while someone is looking
  at it. A spectrum session passes no predicate — drawing is why it holds the radio.
- **`/listen/spectrum?view=band|channel|all`**, refused with a 400 rather than
  substituted; absent means the session's own `default_view`, so a client that never
  learns about views sees no change. `?view=` rides through `GET /api/sdr/spectrum` to
  the PWA, whose store now holds the two pictures apart (`sdrSpectrum.band` /
  `.channel`) and whose two canvases each take only their own rows.
- **`listen-probe --band`** reports what ELSE was on the air, from the same capture that
  made the audio. That reading was impossible before: a spectrum session and a listening
  session are one dongle apiece.
- **C9 rode along.** `LISTEN_QUEUE_BUFFERS = 16` on the listening path only.
  `radio.QUEUE_BUFFERS = 4` was measured for a HOPPING capture, where a shallow ring is
  the point — what is left in it after a retune is pre-retune data. A listening session
  never hops, so the shallow ring buys it nothing and costs it 41 ms of grace against an
  ffmpeg stall, on a box that also runs LLM inference.

**VERIFIED ON AIR 2026-09-06, and the verification found a defect W3 had just
shipped.** One listening session on 162.550: `engine: iq`, 10.12 fps, zero overflows,
the channel at **34.9 dB SNR** 234 Hz off centre — and *at the same time*, off the same
buffer, a 2.4 MHz band row at 586 Hz bins. The band row reported **eight** signals. Only
one of them was real.

| | carrier | signals reported |
|---|---|---|
| listening, AGC | −7.0 dBFS | 162.550 **+ 7 phantoms** at exactly ±55.5, ±111, ±166, ±222 kHz |
| listening, `--gain 30` | −33.5 dBFS | 162.550 alone, 27.1 dB over, +0.6 kHz |
| `spectrum-probe`, same span, fixed 30 dB | −28.8 dBFS | 162.550 at 28.6 dB over |

A *symmetric comb* around one strong carrier is front-end overload, not a channel plan —
none of those frequencies are on NOAA's 25 kHz raster, and the independent path at a
fixed gain sees none of them. **Listening keeps AGC deliberately** (loudness is the point
there), so a band row off a listening session is measured under a moving gain, and
`peaks.find` did its job perfectly on a reading of the receiver rather than of the air.

Fixed the way `_publish_frame` already handles a channel row: **a row measured under AGC
carries no peaks at all.** The picture is still published — its levels are relative and
`band.gain_db: null` says so — but the *measurement* is withheld rather than invented,
because it is what reaches the agent's tools as fact. `Session.tuner_gain_db` is now the
one place the gain rule lives, which is also where two call sites had been computing it
separately.

This is the fifth instance of one pattern in this file's history: **a number that looks
like the quantity and is not.** Median-as-floor (twice), max-as-clipping,
level-as-signal-presence, per-bin-FFT-max-as-suppression, and now
peaks-under-AGC-as-stations.

**Still open, and deliberately not invented here:** the Listen screen does not yet SHOW
the band while it plays. The plumbing is done — one argument (`startSdrSpectrum("all")`)
turns it on — but where that picture goes on the sheet is a DESIGN.md question that
wants a mock, not a canvas dropped in by the wave that made it possible.

**W4 — Retune in place. ✅ shipped 2026-09-06.** A2. `_restart` survives for the
`rtl_fm` fallback and for a `resweep`.

## W4 — what shipped (2026-09-06)

`Session.tune` on the I/Q engine now **moves the radio**: `Capture.swap` takes the
capture's own lock, runs `Radio.retune` inside it, and replaces the sinks. The stream,
the pump, ffmpeg and every listener behind it survive.

**Why a listening retune can do this and a `resweep` cannot** is the line the wave is
drawn on, and it is not "which engine": a listening capture never changes SHAPE. Every
mode captures 2 400 000 samples a second — the property `demod.IF_RATE_HZ` was chosen
around — so a retune changes the tuning and the demodulator and nothing else, even
across a mode change or a crossing of `DIRECT_MAX_HZ` (the ADC branch and the offset
are both `Radio.retune` arguments). A `resweep` changes the rate, the bin count and the
hop plan, which is a different capture. `rtl_fm` takes its frequency on the command
line and has no control channel at all. Both still go through `_restart`, and every
guard in it is still needed — just no longer on the path the owner exercises most.

- **The cost is one dropped frame, ~100 ms, against the ~600 ms of a rebuild.**
  `Radio.read` assembles a frame from several `readStream` calls and `_io_lock` only
  stops a retune landing *inside* one, so the buffer in flight straddles two
  frequencies and is labelled with the one it started on. `Capture.swap` drops it.
- **The order is the reverse of `_restart`'s, and that is the gain.** Everything that
  can fail — validation, then building the new `Demodulator` — happens *before* the
  radio moves, so a request that cannot be served leaves a working session exactly as
  it was. `_restart` kills first and applies second, so anything it raises kills a
  session that was working.
- **A `Radio.retune` that fails ends the session.** It sets rate, branch and frequency
  in order, so a failure partway leaves the radio somewhere nobody asked for — and a
  receiver that keeps demodulating a frequency it is not on is the silent failure this
  whole plan has been peeling.
- **The reaping window is gone, not narrowed.** The incident `alive`'s `_restarting`
  special case documents — a status poll deleting a session that was merely between
  pipelines — cannot occur on a path where the session never has no radio.
- The old station's rows are dropped from `_last`, so a viewer attaching after a retune
  is not seeded with a picture of somewhere else.

**W5 — The subprocess engines go.** B1, then B2/A5 (`SurveySink` emitting the CSV shape the backend already parses), then B7.

**W6 — Filter design becomes a specification.** C12 (Kaiser + a `(pass, stop, atten)` signature), C16, C13, C14, C15, C24.

**W7 — Loose ends.** C8, C17, C18, C19, C20, C22, C23, C25, C26, C28, B4, B5. C21 and C27 need hardware: add probe rungs rather than guessing.


## W1 — what shipped, and what it measured (2026-09-06)

All four landed with five regression tests, each verified to fail on the code before it.

- **C1** `Radio._io_lock` spans `read_into` and the whole of `close()`; `close()` now
  waits for an in-flight read rather than freeing under it.
- **C2** `_build_back` cuts at the midpoint. The chain now tracks the ideal de-emphasis
  curve to **0.1 dB** where it was **1.8 dB** below it at 3 kHz.
- **C5** wide FM builds no channel filter, as its docstring always claimed.
- **C4** `LISTEN_OFFSET_HZ = 126_000` (2.4 MS/s ÷ 19). Narrowband residue **-65 dB at
  0 Hz → -157 dB at +17.7 kHz**.

**C4 is margin, not a visible fix, and the box said so before the change.** An empty
channel's strongest bin already wandered (-9.3, -9.8, +7.6 kHz across three runs), so
the spike sits ~25 dB under the noise floor. Removed while it is cheap; not a fault the
owner was seeing.

**The two test CATEGORIES that were missing** — response against frequency, and where
the DC spike lands — are why C2 and C4 survived. Every previous audio test used one
tone at 1 kHz, where C2 is 0.9 dB. `test_full_deviation_reaches_most_of_full_scale` then
broke on the fix and had to move to the settled audio: it read `Audio.peak` over the
whole buffer, which is the filter's own step response, and had been passing only because
the sag held that transient under 1.0. Third time that trap has fired in this file.

## W2 — what shipped (2026-09-06)

- **B3/C3** `peaks.find` is vectorised: **1910 ms → 2.81 ms** on the FM dial, 238 → 2.73
  on a wideband stare, 7.0 → 0.26 on a listen row. The baseline is a slowly varying
  function of frequency, so it is evaluated every `width/32` bins and interpolated —
  measured at **0.07–0.12 dB** against the exact rolling percentile, a hundredth of
  `SNR_DB`.
- **C6** the window is clamped to a third of the row and the statistic moved from the
  median to the 35th percentile. Run against the REAL CSV that missed NOAA on air, it
  now finds 162.550 at 9.7 dB. The test that pinned the old behaviour is replaced by its
  opposite.
- **C11** 50% Welch overlap. `segments` now counts overlapping windows, which is what
  it is used for (averaging depth).
- **C10** the DC bin is excised on wideband rows only — **three** bins, because a
  periodic Hann puts a bin-centred tone into both neighbours at −6 dB and excising one
  leaves a narrower phantom. Opt-in, because on a channel row DC is the station.
- **C7** `engine` is set on every path and `server.py` stops reading a private attribute.
- **B6** `server.py`'s duplicate `MODES`/`MIN_HZ`/`MAX_HZ` import from `listen`, and the
  dead `WBFM_SAMPLE_RATE` that contradicted it is gone.

### The W1 review, and what it cost

An adversarial review caught that **C4 and C5 combined into a 52 dB regression on wide
FM** — the exact quantity C4 exists to improve. C5 is **withdrawn**: it deleted wide FM's
channel filter on the strength of a docstring sentence that is false (the front end's
6 dB points are at 240 and 120 kHz, not 90, so it passes 113.7 kHz at about −5 dB).
C4's constant was rescored on **suppression** rather than position — my reported −67 dB
for 126 kHz was 56 dB out, because the metric was a per-bin FFT maximum with
unnormalised segment accumulation rather than an RMS. **The fifth instance in this
project of a metric that looked like the quantity and was not.** 300 kHz ships.

C1 was also incomplete: `_io_lock` covered `readStream` only, leaving the same
use-after-free reachable through `retune` on a live stream. It is the device lock now.

## Risks

- **W3/W4 touch the session lifecycle**, whose every guard documents a measured incident (a reaped session mid-retune, an orphaned `rtl_fm` holding a dongle for hours). The refactor must be behaviour-preserving and land *before* the behaviour change, in that order.
- **W5 removes the fallback that CLAUDE.md #10 exists to protect.** It is defensible only because the I/Q engine is now proven on air and because the failure mode it replaces — a picture on the wrong scale, read by an agent as fact — is worse than an honest refusal. The `rtl_fm` **listen** fallback stays.
- **The gain default (`MEASURING_GAIN_DB = 30`) is measured on one box, one antenna.** C22 is what makes that safe to revisit later.
