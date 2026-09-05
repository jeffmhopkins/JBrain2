# SDR I/Q spectrum — own the samples, and shortwave stops being a special case

> **Status:** Proposed · **Last verified:** 2026-09-05 (rev 4) · **Waves:** F0🟡 F1✅ F2✅ F3✅ F4✅ F5✅ F6◻️ F7✅ F8✅ F9◻️ F10🟡

> Reconciled with the root `CLAUDE.md` non-negotiables: no LLM call is added (rule 1);
> nothing new is written to disk (rule 2); no new table, so no new RLS surface (rule 3);
> every operator control stays the PWA surface it is today (rule 10). Rule 8: `supervisor`'s tests load
> `deploy/sdr/*.py` **by path**, so a sidecar dependency IS a dev-env dependency — `numpy`
> goes in `supervisor/pyproject.toml`'s dev extra with a regenerated `uv.lock`. ⟲⟲ The
> second draft also said `scripts/dev-setup.sh` needs updating; it does not — `sync_python`
> runs `uv sync --all-extras` keyed off `uv.lock`, so it picks the new extra up on its own.
> An edit there would be noise. Rule 11 shapes the wave order:
> `deploy/sdr/` is linted and typechecked by nothing and tested by `supervisor`'s pytest,
> so every wave here is verified from `supervisor/`.

Stop parsing `rtl_power`'s CSV and read the radio's **raw I/Q** instead, doing the FFT
here. That is one change to the sidecar's spectrum engine, and it settles four separate
things the current design records as permanent limits.

**This is the fourth draft.** Its three predecessors were independently reviewed. The physics has
survived every pass; the claims about *downstream* code have not, and neither did some of
the corrections. Fixes are marked by the round that found them — ⟲, ⟲⟲, ⟲⟲⟲ — and **six of them were
corrections that were themselves wrong**, which is why the marks are kept rather than
tidied away. Reviews stop here; the remaining risk is the kind only a dongle retires, and
F0 is where that happens.

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
One second is a floor in the C. `bands.py:73` has always said what the fast tier needs —

```
LIVE_FAST = "fast"  # one hop; rtl_sdr + FFT; ~10 fps; the radio never looks away
```

— and **29 sections claim it**. ⟲⟲ Both earlier drafts got this wrong in the same
direction: the first said "nothing branches on `live`", the second said "only `validate()`
reads it". In fact `live` is a **wire field** — declared on `SectionOut` in `api/sdr.py`,
populated in `_section_out`, typed on `BandSection` in `sdrBands.ts`, and shipped to the
PWA on every `/api/sdr/bands` — as well as being branched on twice in `validate()`
(`bands.py:797` and `:803`). It is not free to redefine; it is a published contract with a
frontend type and tests behind it. Every one of those sections
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
   level today is `max(abs(sample))/32768` over **demodulated PCM** (`listen.py:421`) —
   post-discriminator audio loudness, not RF. It is why the squelch at `listen.py:250`
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

**The existing sidecar imports unchanged on 3.13**, so the rebase costs no application
code. ⟲⟲ Two things the second draft got wrong here:

**The size.** "626 MB" was an absolute nobody can act on, and it is not reproducible —
`du -sx /` says 612 MB, `docker images` says 870 MB. The number that matters is the
**delta: +37 MB** over today's image. That is the argument, and it was buried.
⟲⟲ Also worth stating plainly: `python:3.12-slim` **is already Debian trixie** — same
`rtl-sdr 2.0.2-2+b1`, same direwolf, same ffmpeg. This rebase changes the *interpreter*
and nothing else, which makes it far less alarming than "rebase the base image" sounds.
(`python:3.13-slim` + apt `python3-soapysdr` + `PYTHONPATH` would be a smaller diff still,
and is rejected only because it leaves two site trees and two numpys in one image.)

**`bufflen` and `buffers` are NOT settings** — and this one is load-bearing. They come
from `getStreamArgsInfo`, are consumed once inside `setupStream(args)`, and are immutable
for the life of the stream; only `direct_samp`, `iq_swap`, `offset_tune`, `digital_agc`
and `biastee` are `writeSetting` keys. The second draft read a `strings`-style probe that
proved only that the words exist in the shared object, and reported them as one list.

**Why it matters: the default buffer makes 10 fps impossible on the sections this plan
exists to unlock.** `DEFAULT_BUFFER_LENGTH = 16*32*512` = 262,144 bytes = **131,072
complex samples per USB callback**, and a frame cannot arrive faster than one callback:

| section | rate | one buffer | ceiling |
|---|---|---|---|
| VHF sections | 2.4 MS/s | 55 ms | 18.3 fps ✓ |
| `mw` | 1.5 MS/s | 87 ms | 11.4 fps ✓ |
| `80m`, `sw-49m`, `sw-31m`, `sw-25m` | 1.024 MS/s | 128 ms | 7.8 fps |
| `sw-41m` | 300 kS/s | 437 ms | 2.3 fps |
| `40m`, `20m`, `wwv-5`, `wwv-10` | 250 kS/s | 524 ms | **1.9 fps** |

Nine of the ten HF sections land at or below the 1 fps `rtl_power` they replace, and four
are **slower**. Reading with a small `numElems` does not help — `acquireReadBuffer` blocks
until a whole USB buffer lands, then returns `min(bufferedElems, numElems)` and keeps the
rest, so five rows burst out every 524 ms, which is worse on a waterfall than an honest
2 fps.

⟲⟲⟲ **The fix is ONE small `bufflen`, not a per-section one.** `bufflen` is the **USB
transfer size**, not the frame size — nothing requires it to track the rate. A single
49,664 B (97 × 512) works at every rate in the table: 99 ms per callback at 250 kS/s,
10.3 ms at 2.4 MS/s (~100 completions/s across 15 in-flight transfers, trivial for
libusb), and a frame simply aggregates however many callbacks it needs. Three consequences,
all good:

- **Zoom stays live.** A rate change becomes `setSampleRate` alone, with no `setupStream`
  argument to change and so no stream rebuild. The third draft's "honest asterisk" —
  pan live, zoom rebuilds — was an artefact of assuming `bufflen` had to scale.
- **`bufflen_bytes` never enters `bands.py`**, removing one of the two three-layer
  contract changes F4 was carrying.
- ⚠ **But it must be validated, because librtlsdr fails silently.**
  `if (buf_len > 0 && buf_len % 512 == 0) dev->xfer_buf_len = buf_len; else … =
  DEFAULT_BUF_LENGTH;` — a mis-computed value does not error, it **restores the exact
  1.9 fps ceiling this wave exists to remove, invisibly**, and `getStreamMTU` reports the
  *requested* value so it is not a check either. The only honest verification is measuring
  the callback period on the box (F0).

| option | new build tooling | mode 2 | pan / zoom | driver fault | verdict |
|---|---|---|---|---|---|
| **SoapySDR + numpy, trixie-slim** | **none — all apt** | ✅ runtime | **live** / **live** | ⚠ **process-fatal** | **chosen** |
| `rtl_sdr` pipe + numpy | none, but needs pip | ✅ launch-time only | process restart / restart | isolated child | dominated |
| `pyrtlsdr` | none | ✅ | live / rebuild | process-fatal | binds fork-only symbols; see below |
| GNU Radio + gr-soapy | apt | ✅ | live / **flowgraph rebuild** | process-fatal | +759 MB, 242 pkgs |
| SatDump | apt | n/a | n/a | separate process | an app, not a library — but see §8 |
| csdr / pycsdr | source build | ✅ | live / rebuild | process-fatal | not in Debian; apt repo stops at bookworm |
| SDRangel headless | source build | ✅ | live / live | separate process | real REST API, not in trixie |
| OpenWebRX+ | container | ✅ | live / live | separate container | a whole product — see §8 |
| `soapy_power` | pip | ✅ | n/a | separate process | still a sweeper, still text; last release 2019 |

**Why SoapySDR wins, in the order the reasons matter:**

- **Pan stops blanking.** `rtl_sdr -D` can only be reconfigured by dying, so every pan
  and zoom costs 200–500 ms of blank canvas plus a device re-open. `setFrequency` is an
  I2C write on a running stream; zoom rebuilds the stream but not the device.
  ⚠ **But `setFrequency` does not flush the FIFO.** Only `setSampleRate` sets
  `resetBuffer` — `setFrequency` and `writeSetting("direct_samp")` do not. So after a
  retune, `readStream` keeps handing back up to `numBuffers × bufflen` = **1.97 M samples
  of the OLD band** (0.82 s at 2.4 MS/s, 7.9 s at 250 kS/s), which `iq.py` would stamp
  with the NEW `start_hz`. A frame labelled 7.20–7.45 MHz containing 14.15–14.35 MHz is
  the same class of lie `duty` and `uncovered` exist to prevent, and it is worse across a
  `direct_samp` change, where the ADC branch itself moved. **F3 owns a retune barrier.**
  ⟲⟲⟲ The third draft specified it as "discard `rate × settle` samples", which cannot
  work: `settle` is milliseconds and the backlog's *quantum* is one whole buffer. The
  clean fix is free — `activateStream` sets `resetBuffer = true`, zeroes `bufferedElems`,
  and starts the async thread only `if (not joinable())`, so calling it again on a running
  stream is a **pure FIFO flush with no rebuild**. The barrier is therefore two steps:
  re-`activateStream` to drain the software ring, *then* discard `rate × settle` for the
  hardware pipeline `resetBuffer` does not reach. The asymmetry (rate flushes, frequency
  does not) is a fact to design around, not to discover.
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
- ⟲⟲ **It changes the Dockerfile's rule honestly, rather than restoring it.** The second
  draft called this a restoration. It is not: the rule's stated reason is that a sidecar
  "would be carrying a second implementation of what librtlsdr already does", and
  `python3-soapysdr` **is** a Python SDR stack. Installing it by apt satisfies the letter
  and inverts the rationale. The honest framing is that the rule moves from **stdlib
  only** to **apt only, no pip** — a real weakening, defensible on its merits. And it is
  echoed in **six** places, not the two §6.11 counted: `Dockerfile.sdr:6`,
  `listen.py:790`, `server.py:477`, `packets.py:6` and `usbdev.py:15` (both of which say
  "stdlib only" and become false), and `test_sdr_server.py:367`.
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

### What going in-process costs, which the ranking did not price

⟲⟲ Neither earlier draft had a row for this, and it is the real price of leaving the
subprocess model.

**A driver fault becomes process-fatal.** Today a wedged `rtl_fm` is one dead child: the
sidecar keeps serving, keeps the lease registry, and keeps APRS logging on radio B. After
the swap, a segfault in librtlsdr — or a hung `_rx_async_thread.join()` in
`deactivateStream` when `rtlsdr_cancel_async` did not take — kills `ThreadingHTTPServer`,
every lease, and **the APRS session on the other dongle**. Teardown order is load-bearing
and must be stated: `deactivateStream` → `closeStream` → `unmake`, because
`~SoapyRTLSDR()` is a bare `rtlsdr_close` with no stream teardown.

**`/reset` — the owner's terminal-free last resort — stops being clean.** It works today
because the holder is a *child process*: when the lease says nothing holds the radio, no
fd in the sidecar points at the device, so `USBDEVFS_RESET` is safe. With an in-process
handle the leak case is exactly the dangerous one — the lease believes the radio is free
(that is what leaked means), `TUNER.reserve` succeeds, and the ioctl fires **from the same
process still holding the usbfs fd with interface 0 claimed**. The device re-enumerates at
a new node, the orphaned libusb handle goes `ENODEV` and is never closed, and nothing in
`radio.py` learns. `DEBUG_ACCESS.md` promises this is the one recovery that always works;
F3 has to keep that true.

**The orphan story is worse in-process, not better.** `1a64ad0` fixed three defects, not
the one §6.12 named: the `_restart` release race, `_kill` dropping handles it could not
confirm dead (hence `_survivors`), and `capture` SIGKILLing rtl_fm on every run. The
second of those is the one with the bad analogue here — a leaked child is reaped on the
next sweep; **a leaked device handle lives for the container's lifetime with no reap
path at all.**

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

⟲⟲ **The 300–900 kHz legal gap forces a choice on several sections.** `sw-49m` is a
300 kHz span, so 300 kS/s fills 100% of its passband with zero margin — against this
project's own 83% standard — and ⟲⟲⟲ **900 kS/s does not exist** — `rtlsdr_set_sample_rate` rejects `(rate > 300000 && rate <= 900000)`, so 900,000 is inside the excluded band and throws. The third draft stated the legal ranges correctly and then used an illegal rate twice. The usable neighbour is **1.024 MS/s**, which is also exactly achievable.
Every HF section needs that decided, not just `mw` and `20m`. ⟲⟲ And the plan must say
whether a frame is the **passband** or the **section**: at 1.5 MS/s `mw`'s capture reaches
1.865 MHz, so the whole CB image is inside the frame even though it is outside the
section's declared edges.

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
⟲⟲ "Settled" is still one notch too strong: the same function also writes
`demod_write_reg(dev, 1, 0x15, 0x00)` — *disable spectrum inversion* — and neither draft
read it. The NCO derivation is almost certainly right; the confidence was not earned, and
F0 confirms it for the price of one WWV capture it is taking anyway.

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

**The wire.** A 4096-bin frame serialises to **28,750 bytes**. ⟲⟲ The second draft
turned that into "2.30 Mbit/s per viewer" and proposed a compact binary encoding to fix
it. Both are wrong, because neither draft read the proxy: `deploy/Caddyfile:13` is
`encode zstd gzip`, in the same snippet as the `flush_interval -1` that makes SSE work at
all. Measured on a realistic frame, gzip takes 28,823 B → **5,704 B**, so the real cost is
**~0.46 Mbit/s per viewer** — still 16× today's, still worth a budget, not an emergency.
⟲⟲⟲ The third draft then claimed the binary remedy was "2.6× *larger*" — committing the
same raw-vs-compressed error it had just corrected, in the other direction. Measured
properly (deflate-6, `Z_SYNC_FLUSH` per frame): JSON 28,784 B → **4,238–5,529** gzipped;
float32 16,384 B → **3,497–4,638**; int16 deci-dB → 3,175–4,244; uint8 half-dB →
1,459–2,345. Float32 is **15–18% smaller** on the wire, not larger — dB values span a
narrow range, so the exponent and high mantissa bits are near-constant across bins. The
conclusion survives on effort grounds (16% is not worth a wire-format change), but the
reason had to change: if a budget is ever needed, **int16 is ~23% and uint8 ~58%**.

The CPU cost was undercounted and mis-scoped: `json.dumps` is 1.03 ms but
`Frame.as_dict`'s `[round(v, 1) for v in self.db]` (`listen.py:517`) is another **1.41 ms**
of GIL-held pure Python — ~2.4 ms/frame **per subscriber**, not 2.69 ms total.

`SPECTRUM_QUEUE = 4` gives **0.4 s** at 10 fps, and `_publish_frame` (`listen.py:929`)
drops the **arriving** frame when full, so ordinary WAN jitter punches holes in the time
base. ⟲⟲ Its comment says "two seconds' worth", which is already wrong today — 4 frames
at `SPECTRUM_INTERVAL_S = 1` is four. Both drafts repeated the comment's arithmetic
instead of noticing it. And `api/sdr.py` relays with `async for line in
upstream.aiter_lines()` on the **main API event loop**.

None of that is optional work, and none of it is fixed by changing the sample source.

## 6. Corrections carried from the independent review

1. **CI.** `supervisor/tests/test_sdr_listen.py` loads `deploy/sdr/listen.py` by path **at
   module scope**, and `supervisor/uv.lock` has no numpy. A top-level `import numpy` — or
   `import SoapySDR`, which is apt-only and absent from the dev env — fails collection of
   the whole file. Hence `radio.py` and `iq.py` as separate modules (§7 F2/F3).
2. **Two sidecar guards refuse HF independently of the backend**: `listen.py:437`
   `Sweep.of` and `server.py:119` `_range_of`. Relaxing only `tuner.py` and `sdrBands.ts`
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
8. **`sameBand` is fragile to a jittering `start_hz`.** `sdrSpectrum.ts`'s `sameBand` gates a reset of the
   history *and the frozen colour scale* (`SdrWaterfall.tsx:99`) whenever `startHz`/`binHz`/`db.length` changes.
   ⚠ This gets **more** dangerous under Soapy, not less: reading back the *achieved*
   sample rate is a feature, and a ±1 Hz flap in a derived `start_hz` would re-blank the
   waterfall every frame. `start_hz` must be computed from the requested integers.
9. **The survey path is safe, and the plan must say so.** `sweep.py` is entirely relative
   — per-bin percentile floors, `p20/p99.5` for the ramp, the same figures
   `sdrWaterfall.ts` uses — so live dBFS alongside `rtl_power`'s dB breaks neither the
   reducer nor the shared colour scale. But F8 would otherwise put two different
   quantities called "dB" in one UI with nothing distinguishing them.
10. **`_drain_tuner_log` prints `[rtl_fm]`** whatever tool ran.
11. **The Dockerfile rule is duplicated** verbatim in `listen.py:790`. Rule 9 means both.
12. **CI does not build `Dockerfile.sdr` at all.** ⟲⟲ The second draft said "the image's
    build gate proves the real one". `ci.yml`'s `images` matrix is `api`, `supervisor`,
    `proxy` — there is no `sdr` entry, and the `supervisor` paths filter covers
    `deploy/sdr/**` but **not `deploy/Dockerfile.sdr`**, so a PR touching only the
    Dockerfile runs no test at all (including `test_deploy_scripts.py`, which asserts on
    its contents). The only place the SoapySDR import is ever proved is a `docker build`
    on the owner's live box during **Ops → Update** — so a broken rebase surfaces as a
    failed update on a box whose owner has no terminal. That is CLAUDE.md #10 inverted:
    the verification story *requires* the failure the rule exists to prevent. F1 adds the
    image build and the path filter.
13. **`rate / N` is not an integer, and `Frame.bin_hz` is typed `int`** (`listen.py:500`).
    Through librtlsdr's divider arithmetic: 2.4 MS/s → 585.9375 Hz at N=4096, 1.5 MS/s →
    366.2109, 250 kS/s → 61.0352. Rounding 585.9375 → 586 puts the top of a 4096-bin frame
    256 Hz out, and `sameBand` compares `binHz` exactly. ⟲⟲ This also collides head-on
    with §6.8's own fix: "use the requested integers" contradicts §3's headline that
    reading back the *achieved* rate makes `bin_hz` exact. One has to go. The clean answer
    was said to be 2.048 MS/s for the VHF sections (500.0000 Hz exactly at N=4096).
    ⟲⟲⟲ **That fix is wrong, and the right one is `N`, not the rate.** Six fast sections
    span exactly 2.000 MHz (`air-tower`, `air-centre`, `2m-repeaters`, `70cm-low`,
    `70cm-simplex`, `70cm-high`); at 2.048 MS/s they fill **97.66%** of the passband —
    the very ratio (97.5%) this plan rejects for `mw`, invoking `LIVE_FAST_MAX_HZ` by
    name. And the rolloff really does track the rate: `rtlsdr_set_sample_rate` calls
    `tuner->set_bw(dev, dev->rate)`. `N` need not be a power of two — pocketfft is fast on
    any 5-smooth size — so **keep 2.4 MS/s and take N = 4000**: `2.4e6/4000` = **600 Hz
    exactly**, the 83% margin intact. §4 already says bin count must be derived from the
    rate; this is that, applied.
    ⚠ Still unsettled for HF: `mw` at 1.5 MS/s achieves 1,500,000.0149 and `20m` at
    250 kS/s achieves 250,000.0004 — the ±1 Hz flap §6.8 warns about. Exactly-achievable
    replacements exist (**1.024 MS/s**, 960 kS/s, 1.2 MS/s, 256 kS/s) and F4 must pick
    from them. ⟲⟲⟲ `mw` should be **2.048 MS/s**, not 1.5: honest window satisfied
    (`R/2 = 1.024 ≤ fc = 1.115`), fill 57%, rate exact, and 15.6 fps with no `bufflen`
    change — it dominates 1.5 on every criterion the plan states.
14. **`MIN_SWEEP_BIN_HZ = 100` clamps silently** (`listen.py:117`, inside `Sweep.of` at
    `:437`), so the `20m` 61 Hz case does not 502 as the second draft assumed — it clamps
    to 100 and the frame's declared `bin_hz` then disagrees with the FFT that produced it.
    Worse than a refusal. ⟲⟲⟲ And it is **five** sections at N=4096, not the one named:
    `40m`, `20m`, `wwv-5`, `wwv-10` (61 Hz) and `sw-41m` (73 Hz).
15. **`SoapySDR::Device::setFrequency(dir, chan, hz)` — the 3-arg overload — writes the
    remainder to `CORR`**, because SoapyRTLSDR lists `{"RF","CORR"}`. `radio.py` must use
    the named overload, `setFrequency(SOAPY_SDR_RX, 0, "RF", hz)`. And
    `rtlsdr_set_direct_sampling` ends by re-applying the *previous* mode's centre, so
    `direct_samp` must be set **before** `setFrequency`, not after.
16. **F2's "u8 LUT → complex64" stage cannot exist on this path.** `setupStream` accepts
    CF32/CS16/CS8 only; the u8→float conversion is SoapyRTLSDR's own C++ loop. The
    measured "1.2 ms for u8 → complex64" was benchmarking the *pipe* design. F2's input is
    a numpy buffer, and the LUT trick belongs to the option not chosen.
17. **The `sweepable()` split has two consumers, not one** — `api/debug.py` calls it for
    the `rtl_power` survey route as well as the live path.
18. ⚠ **A leaked Soapy device handle is the new orphan.** `1a64ad0` fixed a `_restart`
    race that stranded an `rtl_fm` holding the dongle with nothing pointing at it. Moving
    spectrum in-process removes the child process for that path — and replaces it with a
    stream and a device handle that must be closed on exactly the same paths. `radio.py`
    owns that, and the survivor registry has no visibility into it.

## 7. Waves

**F0 — hardware truths. ⟲⟲ Gates F3, not just F7.** The second draft demoted this to
"blocks F7 only" while admitting in the same sentence that live retune is "the claim the
whole ranking rests on". Those cannot both be true: if retune-while-streaming or the
overflow return does not behave as read, `radio.py` and the engine swap are already
written against a premise that failed. "Needs no radio to build" is not "is not
invalidated". With a dongle: Soapy enumerates it and `serial=` selects the right one;
`direct_samp=2` returns Q-branch samples; **frequency and rate change on a live stream**;
`SOAPY_SDR_OVERFLOW` is really reported under induced backpressure; and the retune barrier
(§3) is measured rather than assumed. F1, F2, F4 and F5 need no radio.

⚠ **F0 is blocked on an open that will not happen, and the first theory about it was
wrong.** MEASURED on the box (2026-09-05, SoapySDR **api 0.8.0**): enumeration returns
both dongles — `driver=rtlsdr`, `serial` `77192819` and `09022796`, tuner and product
strings all present — and `make({"driver": "rtlsdr", "serial": "09022796"})`, a filter
matching one of those rows exactly, answers `SoapySDR::Device::make() no match`. It fails
identically with no serial, so it is not the filter and not `serial=` selection.

The first theory was the SWIG call shape, and it is **falsified**: against SoapySDR 0.8.1
with `soapysdr-module-rtlsdr` installed and **no hardware attached**, `Device(dict)`,
`Device.make(dict)`, `Device("driver=rtlsdr")` and `Device(KwargsFromString(...))` all
reach the driver's own factory and raise `No RTL-SDR devices found!` — the DRIVER's
sentence. So `make() no match` is raised before any factory runs, and the call shape
cannot be what separates a working open from this one. The diagnostic in `radio.py` was
rewritten around what can: what the process has **loaded** (`listModules`), and what
`enumerate` answers for the **exact args** `make` rejects. Those two readings split the
remaining space into three fixes that are nothing like each other — a driver module
missing from this process (packaging), a filter that does open (code here), or the same
args answered two ways (inside `make`, and neither of the above).

**F1 — the base rebase, and CI that actually builds it.** `debian:trixie-slim`; `python3
python3-numpy python3-soapysdr soapysdr-module-rtlsdr` beside the existing `rtl-sdr ffmpeg
direwolf`; `python` → `python3` in `CMD` and `HEALTHCHECK`; `python3 -c "import server,
SoapySDR, numpy"` in the build gate. ⟲⟲ **Add `sdr` to `ci.yml`'s `images` matrix and
`deploy/Dockerfile.sdr` to the `supervisor` paths filter** — without both, the first time
anyone finds out the image is broken is the owner's Ops → Update. ⟲⟲⟲ Two qualifications
the third draft missed: the `images` job is `if: github.event_name == 'push'`, so a matrix
entry is a **post-merge** gate, not a PR one — the PR gate is the paths filter (which makes
`test_deploy_scripts.py` run) plus an explicit build step in the `supervisor` job. And the
box does **not pull** this image: `docker-compose.yml` gives the sdr service `build:`, so
it is the one image built on the box. Whether that changes is a separate decision, not a
side effect. ⟲⟲⟲ pyright also needs handling in this wave, or F1 breaks the `supervisor`
job it is meant to protect: `typeCheckingMode = "standard"` makes `reportMissingImports` an
**error**, and deferring `import SoapySDR` into a function body does not help — pyright
resolves those too. It needs an ignore or a stub, and a decision about
`pythonVersion = "3.11"` typechecking a sidecar this wave moves to 3.13. `numpy` into
`supervisor`'s dev extra with a regenerated `uv.lock`, **pinned to the major Debian
ships** so CI does not test a different numpy than production runs. Add `soapysdr-tools`
for `SoapySDRUtil`, the obvious debug-console probe. ⟲⟲⟲ **Nine** copies of the stdlib-only rule corrected, not six: add
`server.py:21` ("zero new Python dependencies" — the docstring of the file that would
import numpy), `Dockerfile.sdr:5` ("the base IS the dependency set"), and
`SDR_RADIO_PLAN.md:205`, which rule 9 covers too. `deploy/sdr` added to `supervisor`'s pyright `include`, closing a gap the
plan otherwise only observes.

**F2 — `deploy/sdr/iq.py`: samples → frames.** Pure numpy, no radio: window, batched FFT,
Welch, dBFS, `.tolist()`. Input is a **numpy buffer**, not a byte pipe (§6.16). Tests
drive synthetic tones — a tone at a known offset lands in a known bin at a known level.

**F3 — `deploy/sdr/radio.py`: the device behind a protocol.** ⟲⟲ **The import must be
DEFERRED, not merely relocated.** A module boundary does not defer anything: the tests
put `deploy/sdr` on `sys.path`, so `listen.py` → `import radio` → `import SoapySDR` at
module scope breaks collection exactly as before. `import SoapySDR` goes inside
`Radio.open()`, or behind a module-scope `try/except ImportError` with a sentinel. Owns:
open/close; `setSampleRate` and reading back the achieved rate; the **named** `setFrequency`
overload; `direct_samp` **before** `setFrequency`; `setupStream` with the per-section
`bufflen`; the **retune barrier**; the overflow return; teardown order (`deactivateStream`
→ `closeStream` → `unmake`); and the `/reset` interaction (§3) — the handle must be
provably released before a reset can be allowed.

**F4 — the band table learns the truth. ⟲⟲ Moved before the engine swap.** Per-section
`sample_rate_hz`, `bufflen_bytes` and bin count, with the Nyquist window, the frame-rate
ceiling and `rate/N` exactness all enforced in `validate()`. Rates chosen so `rate/N`
divides (2.048 MS/s for VHF). The 300–900 kHz legal gap decided for every HF section, not
just two. `mirrored` → `image_start_hz`/`image_stop_hz`, and the new per-engine fields ADDED.
⚠ ⟲⟲⟲ **The predicate flip does not happen here.** Moving F4 ahead of the engine swap
created the exact failure §6.2 exists to prevent: `whyNotLive` disables a row iff
`!surveyable`, so flipping `surveyable` in F4 while `viewable`/`Sweep.of`/`_range_of`
still refuse would **enable ten HF rows that the route answers with a 400**. F4 ships the
fields; F8 flips the predicate.
⟲⟲ These are **three-layer contract changes**, not one-liners: both fields are on
`SectionOut`, typed in `sdrBands.ts`, read by `whyNotLive`, rendered in `SdrBandSheet`, and
covered by tests on both sides.

**F5 — the PWA survives 10 fps. ⟲⟲ Also moved before the engine swap**, because
otherwise F6 ships 737k `shade()` calls per frame to a phone. Decouple paint from arrival;
re-derive `ROWS` and `CALIBRATION_ROWS` from *seconds* and the live frame rate; make
`start_hz`/`bin_hz` stable so `sameBand` cannot flap.

**F6 — the `spectrum` purpose switches engine.** `Frame` unchanged. The wire budget is
decided here against the **post-gzip** number (§5), not the raw one. Overflow and the
retune barrier surface as stream conditions the way `dutyNote` surfaces hop cost.
⟲⟲ Ships behind a **runtime fallback to `rtl_power`** — if the new engine misbehaves on
the box, an owner with no terminal must not need a revert and a rebuild to get a picture
back (CLAUDE.md #10).

**F7 — bin width becomes ours.** ⟲⟲ §6.4 diagnosed that no wave implemented §2.3 and the
second draft still assigned it to none. `live_bin_hz`'s power-of-two ladder, `_span`'s
`chosen_bin`, the `[100, 100_000]` query bounds and the `SPECTRUM_MAX_BINS` check are all
`rtl_power`'s constraints and all still live. Either this wave removes them or §2.3 is a
lie by the plan's own standard.
⚠ **Shipped ahead of its engine, and corrected after review.** F7 landed while F6 had
not, so `_span` was handing `rate / N` — 250-600 Hz — straight to `rtl_power -f
start:stop:bin`, which honours it: `air-tower` went from 512 bins a frame to 4096, and
`csv_dbm` prints `i1..i2` INCLUSIVE and then repeats `avg[i2]`, so the block is **4097**
columns — ~29 kB per frame instead of ~3.6, relayed on the api's event loop and rounded
value by value in pure Python, for a width the tool labels `%.2f` (488.28 read back as
488, the frame ~1150 Hz out at the top). The table keeps the exact width; `_span` holds
it back behind `api/sdr.py`'s `SPECTRUM_ENGINE_IS_IQ`, which **F6 flips and then
deletes** along with the ladder on the one-hop tier.

**F8 — HF goes live.** ⟲⟲⟲ **Six sites, of which four are independent.** The third
draft said five and missed the one that matters most. Independent gates: `tuner.sweepable`;
`listen.Sweep.of`; `server._range_of`; and `SdrTunerSheet.tsx:47`, which hardcodes
`MIN_MHZ = 24` and refuses with "This radio tunes 24-1766 MHz" — a duplicated tuner floor
of exactly the kind `tuner.py`'s own docstring says caused this bug class. Derived, and
free once `sweepable` is split: `tuner.viewable` (it calls `sweepable`) and
**`sdrBands.ts:100` `whyNotLive`** → `SdrBandSheet.tsx:112`, which is the refusal that
makes the feature *invisible* rather than merely broken — every other site at least
produces an error the owner can see. Per purpose, so the `rtl_power` survey keeps
refusing.

**F9 — stop calling loudness a signal level.** The spectrum path reports true dBFS; the
listening path keeps `rtl_fm`, so its `peak` stays audio loudness and is **labelled** as
such. The two dB quantities are distinguished in the UI (§6.9).

**F10 — the agent's half.** Everything above gives the BOX a capability. jerv reaches
almost none of it, and two of the three tools it does have now describe a machine that
has not existed since `APRS_CONTROL_PLAN` P0b shipped.

*First, the corrections — these are wrong today, independent of this plan.*
`sdr_listen.tool` v3 tells the model "**The box has ONE tuner, so this takes it**" and
that the jobs are "listening, or logging APRS". The box has two dongles, the lease is
per-radio, and `sdrtools.py:113` already resolves per radio through `resolve.for_purpose`
— so the prose contradicts the code it documents, and jerv will report a radio busy
while the other one is idle. There are also **four** purposes now; a refusal naming
`survey` or `spectrum` is a job the description has never heard of. ⟲⟲⟲ It is **three of three**, and the one this draft missed is the
worst: `sdr_aprs_logging.tool` says "APRS logging RESERVES THE RADIO. **The box has one
tuner**, so while it is logging **nothing can be listened to**" — while its own handler in
`sdrtools.py` already returns the opposite ("another dongle, if this box has one, is still
free"). That is the sharpest description-contradicts-code case in the surface. The same
false sentence is in `sdrtools.py`'s own module docstring, in the very file cited here as
proof the code resolves per radio. ⟲⟲⟲ `sdr_stop` is over-charged by comparison: its
*handler* already degrades correctly, reading `holding` and asking which job to turn off.
Only its prose is wrong.

*Second, the gap.* jerv has the CONTROL half of the radio and none of the MEASUREMENT
half. It can tune, log packets and stop; it cannot say what bands exist, cannot sweep,
and cannot tell which radios are attached or what each is doing. So it guesses
frequencies from training data against a regional band plan it has no way to check,
while `bands.py` sits beside it with 29 curated sections carrying mode, channel spacing,
duty cycle and a note about what actually lives there.

*Third, the shape.* `TOOL_CATALOG_PLAN` W1 shipped the opposite work, collapsing read
families into umbrellas and validating it on the live model through
`/api/debug/tool-probe`. ⟲⟲⟲ Do not quote its 48→37 figure — that doc says so itself
("re-measure before W0b rather than quoting either number"), and the measured count at
HEAD is **48**: growth has put jerv back AT the pre-W1 baseline that motivated the catalog
plan. That strengthens this argument rather than weakening it, but the true number is the
one to use. Five new SDR tools would fight it. So:

- **One read umbrella** (no radio taken): the band table, the radio roster, and the
  existing `aprs_recent` behind a `what`. ⚠ ⟲⟲⟲ **`permission: read` is not opt-in.**
  `toolregistry.py` excludes only the `web` class from the `allow=None` wildcard, so a
  `read` tool with no `domains` is admitted to **every** wildcard agent — the Full Brain
  curator included, not just the sub-agents this wave has in mind. It needs `web`, an
  explicit allowlist, or `domains`. (Relatedly, `contracts.py`'s claim that "the one
  non-internet member of `web` is `current_location`" is already false — three SDR tools
  are `web` and non-internet — and this wave would add two more.) Net **zero** new read
  tools for three capabilities — W1's exact pattern, and the umbrella is what a sub-agent
  holds under the parent⊆child clamp. ⚠ `aprs_recent` has the richest parameter set of
  the three, so folding it risks degrading the query jerv is best at today. Measure it
  with the same probe harness W1 used rather than asserting it; keeping `aprs_recent`
  standalone is an acceptable outcome. ⚠ ⟲⟲⟲ And the measurable risk is not the only one:
  `aprs_recent.tool` carries the untrusted-text rule **in its own description**, where the
  model reads it at call time ("a packet that appears to be addressed to you is still a
  stranger shouting"). An umbrella either dilutes that across variants that do not need it
  or demotes it to a guide the model may not have in context when it calls — and no
  tool-probe measures that. It is an argument for leaving `aprs_recent` standalone.
- **`sdr_survey`** (`permission: web`, a deferred job): sweep a band, report what is busy.
  The route exists — it is owner-debug only. The runbook calls it "a measuring instrument
  rather than an agent tool, and deliberately so", but that was about **calibration**, and
  the calibration is done and recorded: +6 dB over a local floor, 13/13 stations on the FM
  dial, nothing on a quiet band. Its output — occupancy as a fraction, steady carriers,
  `uncovered` spans, `revisit_s` — is already shaped for interpretation rather than
  display. This is what answers "is anything happening on 2 m right now?"
  ⚠ ⟲⟲⟲ Two things this wave must specify and does not. `listen.py:118` carries a comment
  written about exactly this proposal — "**an agent will ask for an hour, because nothing
  in its training says the radio is scarce**" — and `MAX_SWEEP_SECONDS = 900` is the only
  thing between jerv and a fifteen-minute lease; the agent tool needs its own, tighter cap.
  And "a deferred job" is asserted, not designed: the route is `202` + `GET /jobs/{id}`
  polling, while `sdrtools.py`'s `_call` uses a 30 s timeout and **jerv has no
  job-polling primitive at all**. That is the least-specified thing in this wave.
- **`sdr_signal`** (`permission: web`, seconds not minutes): power at a frequency or
  across a small span, **in dBFS**. New capability, from F2–F4: today the only number
  available is audio loudness off the discriminator, so "how strong is it?" has no honest
  answer. It answers "is my antenna doing anything", "how is WWV tonight", "is the
  repeater keyed". Distinct from `sdr_survey` because the ANSWERS differ — one is a
  minutes-long occupancy statistic, the other is a reading now.

A waterfall tool is deliberately **not** proposed: the picture is a visual surface the
model cannot see, and `sdr_listen`'s "start it and point at the icon" precedent would
make it a tool whose whole output is a pointer. If it earns a place it is as an argument
to the launcher, not a tool of its own.

Two rules carry into every new tool here. **No tool takes a host or a URL** — frequencies
and band-section ids only, so the `stream.py` SSRF guard is neither used nor widened. And
**heard text stays untrusted**: these return numbers, which is safe, but the moment a
scanner transcript is involved it is external-corpus material and may never reach a model
as instructions (`APRS_CONTROL_PLAN`'s two-tier rule). A packet becoming a prompt is
prompt injection with an antenna.

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
Nyquist zone. No table row lives there. ⟲⟲ The second draft blamed `defaults_for()`,
which has **no production caller at all** — it appears only in `bands.py` and its unit
test — and imagined a hand-typing surface that in fact refuses below 24 MHz
(`SdrTunerSheet.tsx:47`). The real path is `api/sdr.py`'s `Query(ge=TUNABLE_MIN_MHZ)` at
0.1 MHz → `listen.validate` → `demod_args`, plus the debug listen route. F4's window rule
keeps the *table* honest; those two entry points need the same check.
⟲⟲⟲ **The backend half is now fixed rather than deliberately left**: `tuner.aliased`
names the hole, `out_of_range` carries it (so `viewable`, `sweepable` and jerv's tools
inherit it), and every door that tunes — `/sdr/listen`, `/sdr/tune`, `/sdr/aprs`, and
the debug listen and capture twins — refuses with the frequency that would really have
arrived. Refusal, not a caveat: nothing downstream of `-E direct2` can tell 18.1 MHz
from the 10.7 MHz it delivers.
