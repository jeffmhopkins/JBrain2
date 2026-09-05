# SDR I/Q spectrum — own the samples, and shortwave stops being a special case

> **Status:** Proposed · **Last verified:** 2026-09-05 (rev 4) · **Waves:** F0✅ F1✅ F2✅ F3✅ F4✅ F5✅ F6✅ F7✅ F8✅ F9✅ F10🟡

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

⟲ **And the error itself was then reproduced away from the box, which narrows it much
further.** On that same 0.8.1 with the module present and no hardware:

| asked | answer |
| --- | --- |
| `{"driver": "nosuchdriver"}` | `SoapySDR::Device::make() no match` |
| `{"driver": "rtlsdr", "serial": "09022796"}` | `rtlsdr_get_index_by_serial(09022796) - -2` |
| `{"serial": "09022796"}` | `no driver specified and no enumeration results` |

`make() no match` is what a **driver name nobody ever registered** raises. A registered
driver that simply has no hardware raises its OWN sentence instead, and a filter with no
`driver` key raises a third. So the box's answer says the `rtlsdr` **factory** is not
registered in that process — which cannot be squared with enumeration returning two
rtlsdr rows from the same process, and that irreconcilability is the finding rather than
a loose end. The probe therefore carries a **control**: a deliberately bogus driver name
alongside the real one. If the two answers are identical, `rtlsdr` is as unregistered as
a name nobody wrote, and no filter and no call shape can fix it. `getLoaderResult` is
read for the same reason — a module can be listed and have registered nothing.

✅ **ANSWERED on the box, and the call-shape theory was right after all — my
falsification of it was the wrong inference.** With both dongles attached and the
module registered:

| asked | `enumerate` | `make` |
| --- | --- | --- |
| `{}` | 2 | **opened** |
| `{"driver": "definitelynotadriver"}` (control) | 0 | `make() no match` |
| `{"driver": "rtlsdr"}` | 2 | `make() no match` |
| `{"serial": "09022796"}` | 1 | `rtlsdr_get_index_by_serial() - -3` |
| `{"driver": "rtlsdr", "serial": "09022796"}` | 1 | `make() no match` |
| the enumeration row handed back | 1 | `make() no match` |
| **`Device("driver=rtlsdr,serial=09022796")`** | — | **opened** |

`SoapySDRUtil --find=driver=rtlsdr` lists both devices and probes both tuners, so the
C++ side was never in doubt. **A dict carrying a `driver` key is what fails, and it
fails only inside `make`** — the same dict enumerates both devices, and a dict with no
`driver` key opens. The binding's dict typemap therefore yields a value that compares
equal in one code path and not the other; the args-STRING form, which SoapySDR parses
in C++ for itself, sidesteps it. `_Soapy.make` is pinned to the string.

⚠ **Two readings in the diagnosis itself were wrong, and both are corrected.**
`getLoaderResult` does not return an error string: it maps each driver the module
REGISTERED to that driver's error, and a registered driver's error is empty — the box
returned `{"rtlsdr": ""}` for a module that had just registered rtlsdr, and reading the
keys reported that success as a failure. And the control-matches rule fired on a box
where two other filters had opened, concluding "no factory registered" against its own
evidence; an open that happened now outranks every inference about why others did not.
The lesson is the same one as the call shape: **with no hardware attached every shape
fails identically**, so a sandbox can falsify nothing about which one opens a radio.

### F0's answers, measured on the box 2026-09-05

Both dongles, one probe run each. `ok: true`, no findings — and then a second look at
the one number that was too good.

| claim | answer |
| --- | --- |
| `serial=` selects the radio it names | ✅ `09022796` → index 1, `77192819` → index 0, each matching its enumeration index |
| `direct_samp=2` takes | ✅ requested `2`, read back `2` |
| **frequency changes on a LIVE stream** | ✅ 10.000 → 10.256 MHz, `stream_rebuilt: false` |
| rate changes on a live stream | ✅ 256 k / 1.024 M / 2.4 M, every one exact, no rebuild |
| `SOAPY_SDR_OVERFLOW` really reported | ✅ 1 s of induced backpressure → `reported: true`, 1 overflow, 0 timeouts |
| achieved rate == requested | ✅ exact at all three |
| **`bufflen` took** | ✅ **96.92–96.95 ms measured against 97.0 expected** — not the 512 ms default. The 1.9 fps ceiling this plan exists to remove is real, and it is gone. |

The engine was then confirmed against a band that certainly has signal: at 99.3 MHz the
frame has a floor of **-44.4 dBFS** with a carrier **28.9 dB** over it, and a live
retune to 101.7 MHz lands on a station at 101.693 MHz, **30.2 dB** up. The I/Q path
works end to end on real RF.

⚠ **But nothing is reaching the ADC under direct sampling, and the probe called it a
pass.** At 5.0, 7.15 and 10.0 MHz the capture is byte-for-byte the same reading: peak
exactly on the tuned bin at exactly **+3.0 dBFS**, `floor_db` at **-200.0** — which is
`iq.DB_FLOOR`, the zero-magnitude sentinel — and therefore a reported "203 dB over the
floor". That is a DC delta with no noise floor at all, three times at three
frequencies. A receiver with no noise floor is not receiving.

Two things follow. The probe is fixed: the centre bin is excluded from the peak (every
direct-conversion receiver has a DC offset spike exactly at the tuned frequency, so a
peak there is the receiver looking at itself), `dc_db` is reported separately, and a
frame whose median has fallen to `DB_FLOOR` is a **finding**, not a pass. And the
remaining question is **hardware, not software**: either no HF antenna is on these
dongles, or the NESDR SMArt v5 does not wire the Q branch to its input. §1's claim that
"this hardware wires the Q branch" is prose inherited from `bands.py`, never measured,
and it is now the one F0 answer still outstanding. **F6 does not wait on it** — the
engine swap is proven for everything above 24 MHz, which is where the live waterfall
lives; only the HF half of §4's promise is gated.

⟲ **RETRACTED, and by the engine F6 shipped.** The `spectrum-probe` route added after
F6 puts a real live session on the same dongle at the same frequency and rate, and 40 m
comes back **alive**: `engine: iq`, 96 frames in 3.09 s (**31.07 fps**), 1024 bins of
exactly 250 Hz, floor **-51.5 dBFS** with a peak 6.8 dB over it. Re-running the F0
probe at that same 7.2125 MHz still answers `dead` — a DC spike at +3.0 dBFS with a
median at `DB_FLOOR`.

Both cannot be true of the radio, so one was true of the READING, and the single frame
is the weaker measurement: the streaming engine is also what the owner actually sees.
So the conclusion above — "nothing is reaching the ADC below 24 MHz", "the antenna or a
board that does not wire the Q branch" — **was wrong**, and it was reported to the owner
as a hardware fault before the engine existed to contradict it. `_capture` now reads
ten frames the way the engine does, judges the last, and reports `settled` when the
first was empty and a later one was not; the `dead` sentence no longer names a cause it
cannot know. Whether the HF path needs a settle or something else differs between the
two readings is what the next run measures rather than assumes.

The lesson is the same one this plan has now learned three times: **a reading taken
differently from how the system reads is not a measurement of the system.** The call
shape, the sandbox with no hardware, and now a single frame against a stream.

✅ **ANSWERED, on the fixed probe.** At 7.2125 MHz under `direct_samp=2`, reading ten
frames and judging the last:

    dead: false   settled: TRUE   floor -51.4 dBFS   peak -45.5 dBFS (5.9 dB over)

which matches the streaming engine's -51.5 / -44.7 on the same dongle to within a dB.
The same probe at 99.3 MHz returns `settled: false` and a station 24.7 dB over a
-44.3 dBFS floor, so the flag is specific to the branch rather than firing everywhere.

**So the seventh claim holds too, and the cause is now measured rather than guessed:
the direct-sampling branch needs a moment after it is switched.** The first frame after
the switch has nothing in it; a later one does. Nothing else in this system measured
that, which is why a single frame taken there produced a confident, wrong verdict about
the owner's antenna. `settled` is reported as a finding for exactly that reason —
`ok: false` on that run is the probe saying "read me carefully", not the radio failing.

That completes F0: **all seven claims hold on this hardware**, and the shortwave premise
§1 turns on — that the Q branch really does deliver samples here — is measured true.

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

**F6 — the `spectrum` purpose switches engine.** ✅ **SHIPPED.** The engine choice
travels as a **one-hop capture** the api names — `rate_hz` and `bins`, from
`bands.capture_for` — rather than as a flag both sides must keep in step. `_span`
returns the width and the capture that produces it *together*, because a `rate / N`
width handed to `rtl_power` is the 4097-column frame §6.4 describes; a range with no
single capture sends neither field and hops exactly as before, so a sidecar that
predates this sees the body it always saw. The sidecar's `Sweep` carries it, and
`bin_hz` is **not clamped** when a capture named it — clamping a fact about a transform
to `rtl_power`'s floor is how a frame ends up declaring a width nothing computed
(§6.14). `Frame.bin_hz` widened to `int | float` (§6.13). Two consequences that were
not in the wave as written and are load-bearing: `Session.alive` tested a subprocess,
so an I/Q session — which holds a radio and runs no process — would have been reaped by
`current()` the moment it started; and `_kill` walks processes, so the open `Radio` is
closed by name, since a leaked handle is what `/reset` must not fire under (§6.18).
The fallback is `RadioUnavailable`, a **subclass** of `SdrError` so that where it is
not caught it refuses like anything else instead of becoming a 500.

✅ **MEASURED on the box 2026-09-05**, through `spectrum-probe`, which runs the real
route's own decision rather than a copy of it:

| section | engine | frames | fps | bins × width |
| --- | --- | --- | --- | --- |
| `2m-ssb` (200 kHz, one hop) | **iq** | 96 in 3.01 s | **31.89** | 4096 × exactly 250 Hz |
| `40m` (175 kHz, one hop, HF) | **iq** | 96 in 3.09 s | **31.07** | 1024 × exactly 250 Hz |
| `fm-broadcast` (20 MHz, multi-hop) | `rtl_power` | 4 in 4.0 s | 1.00 | 1032 × 19531 Hz |

Every claim the wave rests on, one reading each. The width is `rate / bins` **exactly**
on both one-hop rows (1,024,000/4096 and 256,000/1024 are both 250). The multi-hop row
is sent no capture and keeps rtl_power's own ladder width, which is the tier split
working rather than a fallback firing. And **31.9 fps against a tool clamped to 1 fps
in its own C** is the ceiling this plan was written to remove, measured gone — with a
-60.1 dBFS floor under it, so those are real frames rather than empty ones.

 `Frame` unchanged. The wire budget is
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

**F9 — stop calling loudness a signal level.** ✅ **SHIPPED.** The spectrum path reports
true dBFS per bin; the listening path keeps `rtl_fm`, so its number stays audio loudness
and is **named** for it — `peak` is `audio_peak` on the sidecar wire, on the debug
capture route, and in the PWA's own type.

⟲ The wave as written said "distinguished in the UI (§6.9)", and both halves of that were
wrong. There is no §6.9 — the corrections section is a numbered list and its item 9 is
about the survey path being safe — and the UI half was already done by DELETION: the
tuner sheet's signal meter, which was the one place this number was ever drawn, came out
before this wave was reached. What was actually left was the wire, where two numbers in
one surface were both called `peak`, in different units, measuring different things.
Renaming is the whole fix, and it is stronger than a label: nothing can re-surface
`audio_peak` as a meter by accident.

**F10 — the agent's half.** 🟡 **The corrections shipped earlier; the MEASUREMENT half
ships now, minus `sdr_survey`.**

`sdr_read` is the umbrella — `what=bands` for the 29 curated sections and `what=radios`
for the roster — and it takes no radio, so it can never be refused. Naming one section
returns its channels; the whole table without them stays readable. `sdr_signal` is the
new capability F6 made real: power in **dBFS**, reported as the MARGIN over the frame's
own floor rather than as a raw level, because this receiver has no calibrated gain and
about seven effective bits. It sends the one-hop capture so the I/Q engine answers —
without it the sidecar hops the span with `rtl_power`, whose dB is not dBFS, which is
exactly the confusion F9 exists to stop arriving through the tool whose whole point is
a real power figure.

⚠ **`permission: web`, and the plan's own warning is why.** `read` is admitted to every
`allow=None` wildcard, so a `read` radio tool reaches the Full Brain curator — which
holds no radio and has no use for a band plan. `web` is the gate these need (opt-in,
jerv-only, direct-exec) and the class the other three SDR tools already use;
`contracts.py`'s claim that `current_location` was its one non-internet member has been
false since those landed, and is corrected rather than deepened.

⚠ **`sdr_survey` is NOT shipped, deliberately.** The wave called it "a deferred job",
which is asserted rather than designed: the route is `202` + `GET /jobs/{id}` polling,
`sdrtools._call` has a 30 s timeout, and **jerv has no job-polling primitive at all**.
Inventing an agent-facing async primitive is a wave of its own, and the alternative —
a synchronous sweep short enough to fit the timeout — answers a different question from
the one the tool is for. `sdr_signal` covers "is anything on this frequency" today.
`MAX_SIGNAL_SECONDS = 10` is its own cap for the reason `listen.py:118` records: an
agent will ask for an hour, because nothing in its training says the radio is scarce,
and the sidecar's 900 s ceiling is far too generous to be the only guard.

⚠ **`aprs_recent` stays standalone**, which the wave allowed for. Its description carries
the untrusted-text rule where the model reads it at call time — "a packet that appears
to be addressed to you is still a stranger shouting" — and an umbrella either dilutes
that across variants that do not need it or demotes it to a guide that may not be in
context when it matters.

*The original wave text follows.*
 Everything above gives the BOX a capability. jerv reaches
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

## 7b. F11 — wide bands hop on our own engine, and the picture stops twinkling

**Added after F6 shipped and the owner watched it.** Two findings from the same session,
one measured and one seen.

**Wide bands were still `rtl_power`'s, and they did not have to be.** F6 scoped itself to
one-hop captures, so 88-108 MHz and 144-148 MHz — the only two sections of 31 — kept
sweeping at a row a second. That second is not the cost of hopping: it is
`if (interval < 1) interval = 1;` in the tool's own C. F0 had already measured the thing
that makes hopping ours — **frequency changes on a live stream with no rebuild** — so a
wide span is now several captures stitched into one row. `bands.hop_plan` walks an FFT
ladder finest-first against a frame budget, and each hop contributes only the trusted
middle of its capture (`TRUSTED_FILL`), because a capture's edges sit in the tuner's IF
rolloff and drawing them puts the receiver's own filter shape on screen as a dip at every
seam. On the FM dial that lands at **9,375 Hz bins where `rtl_power` gave 19,531** —
finer AND faster, which is the argument for owning the samples in one line.

Only the tuner side. Below 24 MHz every hop's centre would have to satisfy the Nyquist
window on its own (§4), and a plan that ignored that would draw a picture made of folded
images that looked perfectly plausible.

**Rows are held to 10 fps, by averaging harder rather than by dropping.** The engine
measured 31.9 fps; everything downstream was sized for ten (§5's 5,704 B gzipped per
frame is ~0.46 Mbit/s there, ~1.45 at thirty-two, plus thirty 4096-element JSON parses a
second on a phone). `segments_for` sizes a row to last `1 / TARGET_FPS`, so the samples
that arrive are all used: discarding them would buy nothing and cost the noise floor.

**And the twinkle was ours.** The owner reported a still picture shimmering as it
scrolled, and it was two compounding resampling faults in the PWA. Horizontally, 4096
bins were handed to `drawImage` to squeeze into ~800 device pixels, so a pixel showed a
bilinear blend of two of the five bins under it — chosen by the filter's phase rather
than by the measurement. Vertically, the ring was drawn as **two** scaled draws split at
the write head, and the head advances every row: both halves' scale factors, and with
them the resampler's phase, changed on **every frame**, so a row whose numbers had not
moved was resampled differently each time.

`reduce` now maps bins to columns here, by **max-hold** — the spectrum-display
convention, and not a preference: a carrier occupying one bin of five is the thing being
looked for, and a mean buries it four fifths of the way into the noise. It gathers per
column rather than scattering per bin, so a narrow band on a wide display cannot come out
striped. The ring is unwrapped 1:1 into a scratch canvas and drawn **once**, at a scale
that does not depend on the head.

#### That fixed half of it, and the claim that it fixed all of it was wrong

The sentence this section used to end on — *"what is on screen can then only change when
the measurements do"* — did not survive the owner looking again. Zoomed in, the picture
still "goes through cycles of pixels changing as scroll happens".

Because **a constant scale factor is not the same as a stable picture.** It fixes the
mapping; the data moves through it. `HISTORY_SECONDS` of a 10 fps stream is 1800 rows and
the box shows a few hundred device pixel rows, so the vertical draw was a ~4:1 downscale —
and every frame each row shifts down one source row, so which four rows a display pixel
blends **rotates**, and a row whose numbers never moved is drawn differently each time.
The period of that rotation is the beat between the two heights, which is exactly the
"cycles" reported.

Which is `reduce`'s own argument, on the axis nobody had applied it to: *the pixel's value
must depend on the measurement, not on a resampling decision.* So the vertical is now a
reduction with a rule, like the horizontal. `stackFor` says how many arriving rows share a
pixel row; they are **max-held** into one (a carrier present in one row of four is the
thing being looked for, and a mean buries it); the ring is the display's height, one slot
per pixel row; and the draw is **1:1 on both axes with smoothing off**. A scrolling
picture is stable in that arrangement and in no other.

No row is dropped to achieve it — a partial group waits in an accumulator and reaches the
picture when it completes, so the three minutes of history survive a display that has no
1800 pixel rows to put them in. It also fixed a pre-layout bug found on the way: the ring
is sized from `clientHeight`, which is 0 until the box is laid out, so the picture was
briefly built one pixel tall and regrouped when layout arrived.

### The settle is measured now, not assumed

`SETTLE_S = 0.05` was chosen when the barrier was written and never checked, and F11
made it the most expensive constant in the engine: a hop's samples are microseconds and
its discard is milliseconds, so this one number decides whether the FM dial redraws
twice a second or twenty times.

`soapy-probe` measures it. Zero discard, then one continuous block, sliced — a stopwatch
made of sample indices rather than of a clock, so there is no scheduling jitter between
a sample and its age. The settled level comes from the same radio moments earlier, and
the transient is where the slice-by-slice level stops differing from it by more than
that level's own deviation.

Two methodological corrections were needed to make it a measurement rather than a
statistic. The deviation is a **MAD**, not a standard deviation, because one wild slice
moves `std` enough to hide the transient it is measuring. And the series is **smoothed
with a running median** first: three sigma over several hundred slices puts a handful
past the line by chance, and taking the last of those reported a whole 62 ms block as a
transient — measured off a fake that had none at all. A real transient is never isolated;
an outlier always is.

The verdict is asymmetric on purpose. **Too short is a correctness fault** — the first
samples of every hop are then the previous hop's, drawn at a frequency they are not on,
and a waterfall built that way is confidently wrong rather than merely slow. **Too long
is only speed**, and it is what a wide band's frame rate is spent on.

#### What it read on the box, 2026-09-05, and the two ways the first run lied

The first run of it, at the probe's defaults, said `settle_ms: 0.0` — and that reading
was worthless for two reasons that are worth writing down, because both are ways a
measurement can look like an answer.

**It measured the wrong radio path.** The default `--mhz 10.0` is below 24 MHz, so the
probe opened on the **direct-sampling branch, where the tuner is bypassed entirely** and
a retune is close to a no-op. A hop sweep never runs there (§4: HF cannot be hopped at
all). The number was real and irrelevant.

**And it measured at the wrong rate.** The default `--rate 256000` makes a 512-sample
slice 2 ms wide, so the stopwatch's own resolution was 2 ms and its `steady_sigma_db` was
2.09 — a tolerance of 6.3 dB, coarse enough to miss a transient whole. The sweep runs at
2.4 MS/s, where the slice is 213 µs and sigma came out at 0.175 dB.

Asked on the path the sweep actually uses — `--mhz 100.1 --rate 2400000` — it answered
`settle_ms: 62.72, worst_ms: 80.0` against 50 ms configured. **`worst_ms` was exactly
`SETTLE_SPAN_S × 1000`.** The block was 80 ms long, so 80.0 did not mean "the radio took
80 ms"; it meant the level had still not gone quiet when the samples ran out. A stopwatch
shorter than the event cannot time it, and a reading equal to its own window is an
artefact of the method rather than a measurement of the radio.

Two changes make it answerable. The span is now **0.4 s**, five times the settle it is
checking. And "settled" is no longer *one past the last slice outside tolerance* — over a
span long enough to contain the transient, something late always wanders out (a station
fading, a gain step), and that rule reads every one of those as the radio still settling,
which is precisely how a reading saturates at its own span. It is now **the first slice
from which the level stays inside tolerance for 10 ms** (`SETTLE_HOLD_S`), which is what
settled means and is still immune to a transient that crosses the line and comes back.
When no such run exists the probe reports `saturated` and says *at least* the span, never
the span as a figure.

#### The number, and the bug it exposes

Measured with the widened stopwatch on the box, 2026-09-05, at 100.1 MHz and 2.4 MS/s,
`saturated: 0` — so these are readings rather than the window:

| | ms |
| --- | --- |
| median settle | **61.2** |
| worst of seven | **132.1** |
| configured (`SETTLE_S`) | 50.0 |
| span (headroom) | 400.0 |

**50 ms is too short, and the consequence is not subtle.** A hop reads `bins × segments`
samples — 1024 on the FM dial, which is 0.43 ms of signal. If ~11 ms of stale data
typically remains after a 50 ms discard (61.2 − 50), then the whole of that 0.43 ms read
is stale: **every hop is drawing the previous hop's spectrum.** The wide-band waterfall
is systematically showing each hop's neighbour, and being fast about it.

`spectrum-probe --section fm-broadcast` on the same deploy: `engine: iq`, `bin_hz: 9375`,
2332 bins — F11's hop planning is doing exactly what it was built to do — **and 1.0 fps**,
which is no better than the `rtl_power` ceiling the engine exists to remove. Eleven hops
in a second is ~91 ms per hop, of which the samples are 0.43 ms. The frame rate is
therefore almost entirely the barrier, and raising `SETTLE_S` to cover the measured worst
case would take the FM dial to about **0.5 fps** — correct, and worse than what it
replaced.

That is why `SETTLE_S` is deliberately **not** changed yet. The two candidate fixes are
opposite, and which one is right depends on a fact not yet measured: whether the discard
is bounded by **real time** (the stale samples are still arriving, so every millisecond
discarded is a millisecond off every hop, and the fix is to have less stale data — a
shallower USB pipeline) or by **memcpy** (they are already captured in buffers and
reading them is nearly free, so the settle can cover the worst case for nothing). Guessing
would cost a deploy to learn nothing, so `soapy-probe` now reports `hop_cost`: a ladder of
barriers timed at 0, 50 and 150 ms, with `setFrequency` and the read timed apart from
them, and a `bound` of `real-time` or `memcpy`. It is a reading, not a finding — both
answers describe a healthy radio.

#### Where a hop's 91 ms goes, and the question it raises

`hop_cost`, measured on the box the same day:

| phase | ms per hop |
| --- | --- |
| `setFrequency` | **32.0** |
| the bare flush (`activateStream`) | 0.02 |
| discard at 50 ms | **49.5** |
| discard at 150 ms | 153.4 |
| read 4096 samples | 0.21 |

`bound: real-time`. Three things follow. The flush is free. The read is free — the samples
are already captured, so a bigger capture costs nothing. And **the discard costs exactly
what it asks for**, which kills the comfortable hypothesis: if the stale data were sitting
in buffers, reading past it would be a memcpy. It is not, so the flush really does empty
the pipeline and the samples arriving afterwards are live.

32.0 + 49.5 + 0.2 ≈ 82 ms, times eleven hops ≈ the 1.0 fps observed. The frame rate is
fully accounted for, and neither term is the signal.

**Which leaves the real question: why is live data still WRONG for 61 ms?** An R820T2's
PLL locks in well under a millisecond. What the stopwatch actually watches is a **level**
— and there is a much better candidate for something that moves a level over tens of
milliseconds after a retune.

#### Nothing in this engine ever set the gain

There is no `setGain` and no `setGainMode` anywhere in `radio.py`, so the tuner runs at
librtlsdr's default, which is **automatic**. That is a defect on its own terms, quite
apart from the settle: a waterfall whose gain moves has a dB scale that means nothing from
row to row, and on a hopped band every seam becomes a gain step drawn as if the band had
changed. `soapy-probe` now reports `gain` and raises a finding when it is automatic.

It is also the obvious suspect for the 61 ms. So the probe asks the same question twice —
`retune_settle` as configured, and `retune_settle_fixed_gain` with the gain nailed down
and handed back afterwards, because a probe that leaves the radio configured differently
from how it found it makes the next reading a lie. If the settle collapses with the gain
fixed, it was never a relock, and the fix is the one a spectrum instrument wants anyway.

**It answered, and it falsified the suspect.** The tuner was already in **manual** mode,
so there was no gain loop to blame, and fixing the gain moved the median from 60.8 ms to
51.4 — a nudge, not a collapse.

| | median ms | worst ms |
| --- | --- | --- |
| as configured | 60.8 | 133.3 |
| gain fixed at 30 dB | 51.4 | 123.7 |

What the run did find is a different defect: the gain reads **0.0 dB, the bottom of the
tuner's 0-49.6 dB range**. Nothing in this engine has ever set it, so that is a driver
default rather than a choice, and it costs every weak signal on every band. It is a
finding now.

#### The next suspect, and the control that can convict it

So what disturbs a *live* stream for 60 ms, when the flush empties the pipeline and an
R820T2 locks in under a millisecond? Note what a hop actually does: it **retunes AND it
flushes**, and every reading so far has done both together, so none of them can say which
half owns the transient.

`settle_after_flush` is the control: the same stopwatch, but the disturbance is
`activateStream` with **no frequency change at all**. If the output is still disturbed
with the radio sitting exactly where it was, then the flush is what every hop waits for,
the tuner is innocent, and the fix is to **stop flushing every hop** rather than to
discard for longer after each one. That would also be the cheap fix, since the flush
itself measures 0.02 ms — it is not the flush's own cost, it is what the stream does
afterwards.

**It answered, and the flush is innocent.**

| disturbance | median ms | worst ms |
| --- | --- | --- |
| a whole hop (retune + flush) | 59.7 | 131.2 |
| **a flush with no frequency change** | **0.0** | **0.0** |

Zero on every trial. So the transient is the **tuner's**, it is real, and it survives
three attempts to explain it away: not USB backlog (`bound: real-time`), not the gain
(already manual), not the flush. A frequency change disturbs this radio's output for ~60
ms typically — *after* `setFrequency` has already spent 32 ms returning.

#### What that means, and what it costs

**50 ms was never enough.** A hop keeps 1024 samples — 0.43 ms of signal — so a hop that
discarded 50 ms of a 60 ms transient was reading the previous hop's frequency almost
entirely. The wide-band waterfall has been drawing each hop's neighbour.

**And it sets an honest floor on a hopping sweep.** FM broadcast is 20 MHz; a hop sees
2.0 MHz (`TRUSTED_FILL`); so a row is ~11 looks, each costing ~32 ms of `setFrequency`
plus the settle. That is about a second per row **on one dongle**, and no amount of
tuning removes it — it is why `2m-all` (three hops) feels fast and the FM dial does not.

So `SETTLE_S` becomes 0.15 — the measured worst case plus margin — but as a **cap on an
adaptive discard**, not as a fixed cost. Paying the worst case eleven times a row is what
would make the band unwatchable; the median is less than half of it. `_discard_until_
steady` reads in 1.7 ms slices and stops when the level has held still for 8 ms, with no
reference level, because the new frequency's level is exactly what is not known yet — the
test is that the level has **stopped changing**, not that it matches anything.

#### The adaptive discard was written, shipped and reverted the same day

It stopped when the level stopped moving, and on the box it took the FM dial from 1.0 to
**2.29 fps** — which was the clue, not the result. That is faster than the arithmetic
allows against a 60 ms settle, and "faster than it should be" is what a discard stopping
early looks like.

Two fakes then said why, and the second is the one that matters. **Silence is perfectly
steady** — the direct-sampling branch delivers empty reads before it starts, and the rule
would have stopped there and handed back the silence as a settled band, the same failure
that once had this probe calling a live radio deaf. That one is patchable, and was
patched. **A transient that steps to a WRONG level and holds is also perfectly steady**,
and that one is not patchable, because it is the rule that is wrong rather than its
constants: a discard cannot know the new frequency's settled level — that is precisely
what it is waiting for — so "has the level stopped moving" is the only test available to
it, and without a reference there is nothing to have *arrived at*.

So the discard is **fixed at 0.15 s**, covering the measured worst case with margin. The
price belongs in the open: eleven hops pay it once each, so the FM dial redraws about
**twice a second**, and making that faster means **fewer hops, not a shorter discard**.
The measurement stayed — `settle_after_barrier` reports what is still disturbed after the
real barrier has run, so this cannot regress quietly. **Verified on the box**: 0.0 ms
median and 7.68 ms worst left behind, and the too-short finding is gone.

#### But the price came in at 0.33 fps, and the arithmetic says why

Correct, and **worse than the `rtl_power` this engine exists to replace**. `2m-all`
(three hops) manages 1.97 fps on the same deploy, which is the same cost seen from the
other end.

Three numbers, all measured, and they fit together too well to ignore:

| | |
| --- | --- |
| USB buffer period | 10.27 ms |
| buffers the driver queues | 15 → **154 ms** |
| retune settle | 61 ms median, **134 ms worst** |

A settle distributed uniformly inside one queue depth is exactly what that describes —
and 60 ms is not what an R820T2's PLL does, which is over in well under a millisecond.
The same figures came back from **both dongles** (61.7 / 133.5 on the second), so it is
systematic rather than one radio.

So the suspect is now the thing this file's own comment already named: *"`buffers` is
left at the driver's 15: the count is what bounds the backlog a retune has to flush."*
If the transient is pre-retune data already handed to the kernel, then the 150 ms discard
is paying for a queue depth nothing here needs, and shrinking the queue buys the frame
rate back **without** giving up the correctness.

`queue_ladder` measures it rather than assuming it: a settle per candidate depth, with
each candidate arg tried **alone** so the answer names which knob moved it — SoapyRTLSDR
spells "how many buffers" more than one way and only one reaches librtlsdr's
`rtlsdr_read_async`. Each rung opens its own radio, because a stream argument can only be
set at `setupStream`. If nothing moves, that is worth knowing too: it would mean a 20 MHz
span really is slow on this hardware rather than slow because of a setting.

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
