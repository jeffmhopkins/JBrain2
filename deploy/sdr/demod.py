"""Complex baseband in, mono PCM out: the demodulator that ends `rtl_fm`.

The sibling of `iq.py`, with the same rule — **there is no radio in this file.** A
buffer of complex samples goes in and int16 audio comes out, so every claim it makes
can be proved against a synthetic signal on a machine with no dongle attached. What
can fail because hardware is absent lives in `radio.py`.

**Why this exists.** `rtl_fm` is a separate process that opens the dongle
exclusively, which is the only reason this box could not draw a picture and play a
sound off one radio at the same time. That was never a property of the hardware —
every SDR application (SDR#, gqrx, SDR++, OpenWebRX) runs one I/Q stream, transforms
it for the display and demodulates the same samples for the audio. Once the sidecar
owns the samples (`radio.py`, `iq.py`), the demodulator is the last piece standing
between us and doing the ordinary thing.

**The output is byte-identical in KIND to what `rtl_fm` wrote**: signed 16-bit
little-endian mono at `AUDIO_RATE`. That is deliberate and it is the whole migration
strategy — `listen.Session._pump_pcm` reads a chunk, measures it, accumulates it and
writes it to ffmpeg, and none of those care where the chunk came from. The level
meter, the segment cutter, whisper captions, the MP3 encoder and the direwolf feed
are all downstream of that one `read`, so they are unaffected by construction rather
than by re-validation.

**Integer decimation only.** Every stage divides exactly or the constructor raises.
A resampler that is approximately right produces audio that is approximately the
right pitch and drifts against the clock — over a long session that is a fault nobody
can hear until it is minutes out. Choosing a capture rate that divides is the
caller's job, exactly as choosing an N that divides the rate is in `iq.py`.

**Two decimation stages, not one.** Going from 2.4 MS/s to 48 kHz in one filter needs
a transition band of 0.7% of the rate and about six hundred taps at every input
sample. Split 50 into 10 and 5 and the first filter only has to reject what would
fold into the band the SECOND filter keeps, which is a transition of 8% and fifty
taps — and it runs at a tenth of the output count. The arithmetic is the same to the
ear and about thirty times cheaper.

**The IF is exposed, and that is a feature.** `Audio.baseband` is the decimated
complex stream, centred on the tuned frequency. An FFT of it is a spectrum of the
channel at the IF's resolution — 512 bins over 48 kHz is 94 Hz, against the 600 Hz a
4000-bin transform of the full 2.4 MHz capture gives. So the narrow tuning view is
not merely possible alongside the audio, it is *better resolved* than a zoom into the
wideband row, and it costs a 512-point FFT. This is the standard "zoom FFT", and
getting it for free is the reason to demodulate here rather than anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

#: whisper's native rate, and what `rtl_fm` was asked for. Changing it changes the
#: caption model's input, so it is mirrored from `listen.AUDIO_RATE` rather than
#: imported: this module deliberately imports nothing from its siblings.
AUDIO_RATE = 16_000

#: The intermediate rate each mode is decimated to before it is demodulated. Narrow
#: modes get 48 kHz — three times the audio rate, wide enough for any channel this
#: radio tunes and cheap to run a discriminator on. Wide FM gets 240 kHz because the
#: station itself occupies ~180 kHz: demodulating it at anything narrower clips the
#: deviation and the audio arrives distorted rather than quiet, which is the failure
#: `listen.WBFM_SAMPLE_RATE` documents at length for `rtl_fm`. 240 rather than that
#: 192 for two reasons — Carson's ±90 kHz reaches Nyquist exactly at 192, leaving the
#: anti-alias filter no transition band at all, and 2 400 000 divides by 240 000 and
#: by 48 000 alike, so every mode this box has can share ONE capture rate.
IF_RATE_HZ: dict[str, int] = {
    "fm": 48_000,
    "nfm": 48_000,
    "am": 48_000,
    "usb": 48_000,
    "lsb": 48_000,
    "wbfm": 240_000,
}

#: FM de-emphasis time constant. 75 µs is the Americas; 50 µs is most of the rest of
#: the world, and `bands.REGION` is what would decide it if this ever travels.
DEEMPHASIS_S = 75e-6

#: The audio low-pass ahead of the final decimation, per mode. Narrow FM and AM voice
#: carry nothing above ~3.5 kHz; wide FM would carry 15 kHz if the output rate could
#: hold it, and at 16 kHz it cannot — 7 kHz is the honest ceiling under Nyquist with a
#: transition band that fits.
AUDIO_CUTOFF_HZ: dict[str, float] = {
    "fm": 4_000.0,
    "nfm": 4_000.0,
    "am": 4_000.0,
    "usb": 3_000.0,
    "lsb": 3_000.0,
    "wbfm": 7_000.0,
}

#: Where an SSB passband STARTS, relative to the suppressed carrier. The telephony band
#: every SSB radio is built around begins at 300 Hz; where it ends is the bandwidth.
SSB_LOW_HZ = 300.0

#: WHY THESE NUMBERS, and it is one measurement.
#:
#: **AM's widest preset is 8 kHz, and its ceiling used to be 16.** MEASURED 2026-09-08 on the real
#: chain: two equal AM carriers 5 kHz apart, and at 16 kHz the neighbour arrives in the
#: audio at 0.0 dB — exactly as loud as the station that was tuned. At 8 kHz it is
#: 14.8 dB down, at 6 kHz 85.2, at 4 kHz 110.0. The 16 kHz filter was not a wider,
#: better-sounding option that the owner might want on a quiet band: AM's audio is
#: low-passed at 4 kHz downstream, so RF beyond ±4 kHz cannot carry any wanted audio,
#: and the same measurement shows 16 kHz and 8 kHz giving IDENTICAL response at every
#: audio tone out to 3.5 kHz. It was 8 kHz of pure interference intake, and dropping it
#: costs nothing that can be measured or heard.
#:
#: **Why narrowing works at all is the ORDER.** This filter runs before the envelope
#: detector, and detection is non-linear: two carriers inside the passband beat against
#: each other and their products land in the audio band, where no downstream filter can
#: separate them from the wanted audio again. Rejection has to happen while the
#: interferer is still a separate signal, which is here.

#: The step a width must land on, and the smallest transition any filter here is asked
#: to build. 100 Hz rather than 1 kHz because the named presets are not whole kilohertz —
#: SSB's 2.4 and NFM's 12.5 both are 100 Hz multiples and neither is a 1 kHz one — so a
#: 1 kHz grid would make the classic filters unreachable by the very control meant to
#: offer them. The PWA's drag snaps to whole kilohertz; this is what the box will accept.
BANDWIDTH_STEP_HZ = 100

#: The widest and narrowest filter each mode will build, as full channel widths.
#:
#: A RANGE rather than only the presets below, because the owner drags the passband edge
#: on the picture and wants it to land where they put it — "narrower than that station"
#: is a position, not a menu choice. The presets stay as the quick picks the ladder
#: shows; the range is what the box accepts.
#:
#: **Each ceiling is its mode's widest preset, deliberately.** On AM that is the whole
#: point: 8 kHz is the widest filter that costs no audio, and anything above it is the
#: pure interference intake the 16 kHz default was — so the range must not reopen a door
#: this wave measured shut. It also keeps the tuning picture exactly the width it already
#: is, since `crop_reach_hz` is taken from this ceiling.
#:
#: The floors are where a filter stops being one: `_build_channel` asks for a stopband
#: `max(0.5 * kept, 2000)` above the edge, so a narrower filter than these would need a
#: transition band wider than its own passband.
BANDWIDTH_RANGE_HZ: dict[str, tuple[int, int]] = {
    "fm": (5_000, 16_000),
    "nfm": (5_000, 16_000),
    "am": (2_000, 8_000),
    "usb": (1_000, 3_100),
    "lsb": (1_000, 3_100),
    "wbfm": (180_000, 180_000),
}

#: The quick picks the ladder offers, widest first. **The first entry is the mode's
#: default.** No longer the whole contract — `BANDWIDTH_RANGE_HZ` is — but still the
#: widths worth naming, and each one a filter a test measures.
BANDWIDTH_HZ: dict[str, tuple[int, ...]] = {
    # 25 kHz and 12.5 kHz are the two channel rasters land mobile actually uses; 8 kHz
    # is for sitting on top of one of a pair when both are busy.
    "fm": (16_000, 12_500, 8_000),
    "nfm": (16_000, 12_500, 8_000),
    # 8 kHz is everything the audio path can use; 6 kHz is the shortwave broadcast
    # raster (5 kHz spacing); 4 and 3 are for a channel with a neighbour on top of it.
    "am": (8_000, 6_000, 4_000, 3_000),
    # 3.1 kHz is the telephony band SSB was built around, 2.4 kHz is what most modern
    # radios call "voice", 1.8 kHz is for digging one signal out of a pile-up.
    "usb": (3_100, 2_400, 1_800),
    "lsb": (3_100, 2_400, 1_800),
    # Not adjustable, and the single entry is how that is said. A broadcast FM station
    # occupies ~180 kHz; a narrower filter clips the deviation, and clipped deviation
    # is distortion, not selectivity — it makes the station sound worse, not cleaner.
    "wbfm": (180_000,),
}


def passband_for(mode: str, bandwidth_hz: float) -> tuple[float, float]:
    """WHERE a mode's passband sits, as (low, high) offsets from the tuned frequency —
    what the demodulator actually hears, and therefore what the tuning view shades.

    **SSB is one-sided, and the strip drew it symmetric** (C14). A half-width cannot say
    this: usb and lsb both shaded ±3400 Hz while the demodulator heard +300..+3400 (usb)
    or -3400..-300 (lsb) — half the shaded box was the sideband the back end rejects,
    and someone centring a signal in it put half the signal where nothing can hear it,
    which is the one mistake SSB tuning most invites.

    So the bandwidth means the same thing to a reader in every mode — how wide a slice
    of spectrum is heard — while WHERE that slice sits is the mode's business."""
    if mode == "usb":
        return (SSB_LOW_HZ, SSB_LOW_HZ + bandwidth_hz)
    if mode == "lsb":
        return (-(SSB_LOW_HZ + bandwidth_hz), -SSB_LOW_HZ)
    return (-bandwidth_hz / 2.0, bandwidth_hz / 2.0)


def channel_half_for(mode: str, bandwidth_hz: float) -> float:
    """The channel filter's half-width — the FILTER, not the passband.

    Symmetric for every mode including SSB, where this runs at the IF rate before the
    back end picks a sideband, so it must reach the far edge of the one that is kept."""
    low, high = passband_for(mode, bandwidth_hz)
    return max(abs(low), abs(high))

#: How much of the IF the FRONT END keeps flat, as a share of the IF rate — 80% of
#: Nyquist, so the picture has honest spectrum either side of the channel.
#:
#: This is the constant that separates the two jobs the front end used to do badly at
#: once. It decimated to the IF with its cutoff set at `CHANNEL_HALF_HZ`, which made it
#: the channel filter as well as the anti-alias one — and a windowed sinc's cutoff is
#: its SIX DECIBEL point, not its passband edge, so "keep 8 kHz" really meant a
#: response sloping from 0 dB at DC to -4.7 dB at 8 kHz, -11 dB at 12 and -21 dB at 16.
#:
#: MEASURED ON AIR 2026-09-06, and it cost the owner an evening. An EMPTY channel of
#: pure receiver noise, shaped by that slope, arrives at the tuning view as a smooth
#: hump centred on the tuned frequency reading 13.7 dB over its own outer bins — which
#: the strip draws as a strong, perfectly centred station and the probe passes as
#: `ok: True`. 162.550 read "on centre, -47.4 dBFS" with nothing on the air at all,
#: while the audio was the static that noise through a discriminator actually is.
#:
#: So the front end is now anti-alias ONLY, flat across this width, and a separate
#: `_build_channel` filter does the selectivity at the IF rate. The picture is taken
#: BETWEEN them, which is what every SDR display does and what makes the shaded
#: passband mean something: you can only see how much of a signal falls outside the
#: filter if the picture is wider than the filter.
VIEW_SHARE = 0.4

#: How much wider than the channel the picture has to be before it can carry a NOISE
#: FLOOR — a piece of the row that the demodulator is not listening to, which is what
#: `_channel_floor` in the sidecar and `floorOf` in the PWA both measure "over the
#: noise" against.
#:
#: This exists because widening the picture was not enough on its own. Wide FM's
#: channel is 180 kHz inside a 240 kHz IF, so even a perfectly flat front end leaves
#: 6 kHz a side outside the passband — and those bins are the station's own skirts.
#: The old sagging filter had been SUPPRESSING them, which by accident made them look
#: like a floor; flattening it handed the floor estimator the signal instead, and
#: 96.5 — a station `rtl_power` sees 13 dB up — came back "nothing is transmitting
#: here". A false negative on a strong station, from the fix for a false positive on
#: an empty one.
#:
#: So when the IF is too narrow to hold both the channel and a margin, the PICTURE is
#: taken further up the chain, at `view_rate_hz`, while the audio carries on down to
#: `if_rate_hz` as before. The alternative — demodulating wide FM at 480 kHz — was
#: measured at 21.7% of a core against 6.1%, because the audio filter's length goes
#: with the rate it runs at. There is no reason the two should share a rate.
VIEW_MARGIN = 1.5

#: How many samples of the wide picture one frame produces — eight 512-bin segments
#: for `iq.Spectrometer` to average over, which is close to what a narrow mode's whole
#: buffer already gives it.
#:
#: The picture is a SLICE of the buffer rather than all of it, and only on the wide
#: path. Filtering all 240 000 samples of a 100 ms capture down to 480 kHz costs a
#: hundred taps at every one of the 48 000 outputs — measured at 20.3% of a core
#: against 6.1% — and 47 900 of those outputs are then dropped, because a 512-bin
#: transform consumes 4 096 samples and no more. Slicing first makes the same picture
#: for a twelfth of the arithmetic. What it costs is honest and worth naming: a wide
#: row integrates over 8.5 ms of the frame rather than all 100, so a burst inside the
#: other 91.5 ms is not in it. For a station one is TUNED TO that is a trade worth
#: making; the wideband waterfall, which exists to catch bursts, still sees every
#: sample.
VIEW_SAMPLES = 4096

#: How long a window the AM carrier is averaged over, in AUDIO samples.
#:
#: MEASURED, because this said 31 Hz and 31 Hz is the wrong quantity (C15).
#: `audio_rate / length` — 31.25 Hz here — is the boxcar's first NULL, where a
#: subtracted average leaves the signal UNTOUCHED (+0.07 dB), not where it is half
#: gone. The real response of `x - boxcar(x)` at 512 taps and 16 kHz:
#:
#:     -3 dB at 7.5 Hz · -0.96 dB at 10 Hz · **+2.00 dB at 20.5 Hz** · 0 dB by 31 Hz
#:
#: The overshoot is the boxcar's own sidelobe showing through the subtraction, and it
#: is inherent to this shape rather than a tuning mistake. Kept anyway: it is 2 dB at
#: 20 Hz, below anything an AM voice channel carries, and the alternative — a one-pole
#: — cannot be vectorised (see `_DcBlock`). What is not kept is the wrong number.
DC_BLOCK_TAPS = 512

#: The AGC, for the modes whose audio level is the SIGNAL's rather than the
#: modulation's (C13).
#:
#: **Why only AM and SSB.** An FM discriminator's output is the DEVIATION, which the
#: transmitter sets and `FM_DEVIATION_HZ` scales to full scale — so FM already arrives
#: at a level that means something, and an AGC on top of it would destroy the one honest
#: thing about it. AM and SSB carry the RF level straight through to the audio, so the
#: same station is as loud as the propagation happens to make it.
#:
#: MEASURED at one RF level: nfm -11.4 dBFS, usb -23.0, am -31.0 — a **20 dB swing on a
#: mode change**, and a weak AM or SSB station simply inaudible. `rtl_fm` behaves the
#: same way, so this was parity rather than a regression; every listening application
#: runs an AGC here.
#:
#: The window is a trailing mean square over a quarter of a second, which is long enough
#: to ride over the syllables of speech and short enough to follow a fade. A boxcar over
#: a carried tail, for the reason `_DcBlock` gives at length: it is O(n) through one
#: cumulative sum and EXACTLY the same whether the samples arrive in one buffer or in
#: forty, which `test_chunking_changes_nothing` holds to two int16 counts.
AGC_WINDOW_S = 0.25

#: What the AGC aims the RMS at. 0.2 is -14 dBFS, which lands speech peaks around 0.6-0.8
#: at a voice's crest factor — measured on air, wide FM through this chain reads 0.24 RMS,
#: so AM and SSB now arrive within a few decibels of it instead of 20 down.
AGC_TARGET_RMS = 0.2

#: How far the AGC may go, in each direction. Up is what makes a weak station audible;
#: down is what a real receiver does with a strong one instead of clipping it. Bounded
#: because an unbounded gain amplifies an empty channel's noise to full scale, which
#: sounds like a fault and hides the fact that nothing is there.
AGC_MAX_GAIN_DB = 40.0
AGC_MIN_GAIN_DB = -20.0

#: Peak deviation each FM mode is scaled against, so a fully-deviated signal arrives
#: at full scale instead of at whatever fraction the IF rate happens to make it. The
#: discriminator's natural output is `2 * f / if_rate`, which for a 5 kHz-deviated
#: narrowband signal at a 48 kHz IF is 0.21 — a correct reading that sounds like a
#: broken radio, and the reason `rtl_fm` applies a gain here too. Over-deviation then
#: CLIPS, which is what an overdriven receiver does and is audible as such.
FM_DEVIATION_HZ: dict[str, float] = {"fm": 5_000.0, "nfm": 5_000.0, "wbfm": 75_000.0}

#: Headroom over full deviation. Without it the scaling above puts a fully-deviated
#: signal at EXACTLY full scale, so a transmitter running a little hot — which is most
#: of them — clips, and there is nothing left for the peaks that carry a voice's
#: consonants. -3 dB is what a receiver leaves; the cost is audio a third quieter,
#: which the player's own volume answers and clipping does not.
FM_HEADROOM = 0.7

#: How far down the stopband has to be, for every filter in this chain.
#:
#: **This is a specification, and it used to be a side effect.** Every filter was
#: Hamming-windowed, whose stopband is a property of the WINDOW (~-53 dB) and not of
#: anything anyone asked for: worst measured leakage into the demodulated channel was
#: **-56 dB**, which a local blowtorch 60-70 dB over a weak station is audible in.
#: Kaiser takes the number as an input, so 80 dB is a thing this chain now promises and
#: `test_lowpass_delivers_the_stopband_it_was_ASKED_for` checks. **After: -79.1 dB on
#: wide FM, -81.3 on SSB**, measured over the whole capture band.
#:
#: It costs +27-29% of the chain's multiply-accumulates, which is exactly what the design
#: formula predicts — (80-8)/14.36 = 5.02 taps per unit transition against the old rule
#: of thumb's 4.0. Twenty-four decibels for twenty-eight percent, on a chain the capture
#: thread already carries at 11.4% of one core.
STOPBAND_DB = 80.0

#: Never fewer than this, whatever the design formula says. A very wide transition can
#: ask for nine taps, and a nine-tap low-pass has a passband that is not flat.
_MIN_TAPS = 31


def _split(total: int) -> tuple[int, ...]:
    """One decimation factor as one or two stages, the coarse one first.

    The trade the split exists to win: a stage's filter length goes with the
    RECIPROCAL of its transition band, and its cost goes with its OUTPUT count. A
    single 50:1 stage pays a long filter at every output; 10 then 5 pays a short
    filter at a tenth of the rate and a longer one at a fiftieth.

    The first factor is aimed a little above the square root, which is where those two
    terms balance for the ratios this radio actually uses, and rounded to a real
    divisor so the arithmetic stays exact. Small ratios stay in one stage — two
    filters to decimate by six costs more than the one it saves."""
    if total < 8:
        return (total,)
    reach = int(total**0.5 * 2)
    first = max(d for d in range(2, reach + 1) if total % d == 0)
    return (first, total // first) if first != total else (total,)


class DemodError(ValueError):
    """A mode or a rate this module cannot honestly serve."""


def _kaiser(atten_db: float, transition_hz: float, rate_hz: float) -> tuple[int, float]:
    """Kaiser's own formulas: how many taps and what β buy `atten_db` of stopband.

    Both are empirical fits Kaiser published, and they are why this window is the one
    worth having — the shape is a parameter rather than a constant, so a stopband is
    something a caller ASKS for instead of something a window happens to give."""
    a = float(atten_db)
    if a > 50.0:
        beta = 0.1102 * (a - 8.7)
    elif a >= 21.0:
        beta = 0.5842 * (a - 21.0) ** 0.4 + 0.07886 * (a - 21.0)
    else:
        # Below 21 dB the window is a rectangle and β is meaningless; the formula above
        # would go complex on the fractional power.
        beta = 0.0
    n = int(np.ceil((a - 8.0) * rate_hz / (2.285 * 2.0 * np.pi * transition_hz))) + 1
    return max(_MIN_TAPS, n | 1), beta


def lowpass(pass_hz: float, stop_hz: float, atten_db: float, rate_hz: float) -> np.ndarray:
    """A low-pass stated as a SPECIFICATION: flat to `pass_hz`, `atten_db` down by
    `stop_hz`. Odd length, linear phase, unity gain at DC.

    **The signature is the point** (C12). It used to be `(cutoff_hz, rate_hz, taps)`,
    and a windowed sinc's `cutoff_hz` is its 6 dB point — the MIDDLE of the transition,
    not the edge of the passband. "Is cutoff the edge or the 6 dB point?" produced two
    separate bugs in this file: `_build_front` passed the passband edge (a response
    5 dB down at the edge of the band it claimed to keep) and `_build_back` was still
    doing it after the front end was fixed, costing 1.9 dB at 3 kHz on AM. Taking the
    two edges and deriving the midpoint HERE makes the question unaskable — there is no
    longer a parameter anyone can misread.

    The tap count comes with it, from `atten_db` and the transition width, because a
    caller computing taps separately is the same mistake wearing a different hat.

    Odd length so the filter is symmetric about a whole sample and its delay is an
    integer — an even-length linear-phase filter delays by half a sample, which is
    harmless alone and becomes a half-sample of skew when two chains are compared.

    Normalised by its own sum rather than analytically: the truncation and the window
    both cost a little DC gain, and a filter that quietly attenuates by 0.4 dB per
    stage is a level error that compounds across the chain and shows up as a level
    meter that disagrees with the spectrum."""
    if pass_hz <= 0:
        raise DemodError(f"a passband edge must be above 0 Hz, not {pass_hz}")
    if stop_hz <= pass_hz:
        raise DemodError(f"stopband {stop_hz} Hz must be above passband {pass_hz} Hz")
    if stop_hz > rate_hz / 2:
        # Not a clamp. A stopband above Nyquist is a request the sample rate cannot
        # carry, and silently moving it would hand back a filter that does not do what
        # the caller asked while looking like one that does.
        raise DemodError(f"stopband {stop_hz} Hz is above Nyquist ({rate_hz / 2} Hz)")
    n, beta = _kaiser(atten_db, stop_hz - pass_hz, rate_hz)
    cutoff_hz = (pass_hz + stop_hz) / 2.0
    k = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
    # np.sinc is the NORMALISED sinc — sin(pi x)/(pi x) — so the argument is in
    # cycles, not radians. Passing 2*pi*fc/fs here is a classic and silent error: the
    # filter comes out with a cutoff 2*pi times too high, which at these ratios is
    # simply no filter at all.
    h = 2.0 * (cutoff_hz / rate_hz) * np.sinc(2.0 * (cutoff_hz / rate_hz) * k)
    h *= np.kaiser(n, beta)
    return h / h.sum()


def deemphasis(rate_hz: float, tau_s: float = DEEMPHASIS_S) -> np.ndarray:
    """The de-emphasis one-pole, as an FIR of its own impulse response.

    A one-pole IIR is three lines and cannot be vectorised — each output needs the
    one before it, so it is a Python loop over every sample at the IF rate. Its
    impulse response is `(1-a) * a**n`, which at 48 kHz and 75 µs decays below
    float32's resolution inside fifty taps, so the FIR is not an approximation in any
    sense the arithmetic can tell. It also CONVOLVES with the anti-alias filter it
    sits next to, which is how both end up costing one pass instead of two.

    **It stays at the IF rate, and that was measured rather than assumed** (C16). Run at
    the 16 kHz AUDIO rate as `gr-analog` does, this curve is 2.8 dB off the ideal analog
    shape by 7 kHz (10.4 dB for gr-analog's own bilinear design, whose frequency warping
    is severe that close to Nyquist) — where here it is 0.012 dB. gr-analog gets away
    with it because broadcast FM there lands at a 48 kHz audio rate. The saving was
    4.6 Mmac/s out of 225, against a curve W1 spent a wave getting right."""
    a = float(np.exp(-1.0 / (tau_s * rate_hz)))
    # Long enough that the tail is below the quantisation of the int16 it becomes.
    length = max(8, int(np.ceil(np.log(1e-7) / np.log(a))))
    h = (1.0 - a) * a ** np.arange(length, dtype=np.float64)
    return h / h.sum()


class _Fir:
    """A decimating FIR that remembers where it was.

    Two pieces of state, and leaving either out is a defect you HEAR. The tail is the
    filter's memory: without it every buffer boundary is a discontinuity, which is a
    click ten times a second. The phase is which input sample the next output is due
    on: without it a buffer whose length is not a multiple of the decimation restarts
    the output grid, and the audio picks up a fractional-sample jitter that reads as
    roughness rather than as clicks — the nastier of the two, because it sounds like a
    bad radio instead of like a bug.

    The kept outputs are gathered with a strided window rather than convolving
    everything and throwing away `m - 1` of every `m`: the work is then proportional
    to the OUTPUT count, which is the whole reason to decimate in stages."""

    def __init__(self, taps: np.ndarray, m: int, *, complex_in: bool) -> None:
        if m < 1:
            raise DemodError("decimation must be at least 1")
        # Reversed once here so the hot path is a dot product rather than a
        # convolution. Correct for asymmetric taps too — the SSB bandpass is complex
        # and only conjugate-symmetric, so `taps[::-1]` is not `taps`.
        self._taps = np.ascontiguousarray(taps[::-1])
        self._m = int(m)
        self._dtype = np.complex64 if complex_in else np.float32
        self._tail = np.zeros(self._taps.size - 1, dtype=self._dtype)
        self._phase = 0

    def feed(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self._tail, x.astype(self._dtype, copy=False)])
        valid = buf.size - self._taps.size + 1
        if valid <= self._phase:
            # Not enough for one output yet. Keep everything: dropping the head here
            # would lose samples outright, which a short first buffer makes routine.
            self._tail = buf
            return np.zeros(0, dtype=self._dtype)
        window = sliding_window_view(buf, self._taps.size)[self._phase :: self._m]
        out = window @ self._taps
        self._phase += window.shape[0] * self._m - valid
        self._tail = buf[valid:]
        return out.astype(self._dtype, copy=False)


class _DcBlock:
    """A trailing moving average, subtracted: a high-pass whose -3 dB corner is about
    `0.24 * rate / n` — see `DC_BLOCK_TAPS` for the measured response and for the
    number this used to claim.

    This is the AM carrier remover, and it is a boxcar rather than the obvious one-pole
    for two reasons. A one-pole cannot be vectorised — every output needs the one
    before it — so it is a Python loop over every audio sample. And the per-buffer
    smoothed mean that stood here first was WRONG rather than merely slow: its state
    advanced once per BUFFER, so the same samples handed over in forty pieces
    converged forty times faster than in one, which `test_chunking_changes_nothing`
    caught. A boxcar over a carried tail costs O(n) through one cumulative sum and
    depends on nothing but the samples."""

    def __init__(self, length: int) -> None:
        self._n = max(2, int(length))
        self._tail = np.zeros(self._n - 1, dtype=np.float32)

    def feed(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        buf = np.concatenate([self._tail, x.astype(np.float32, copy=False)])
        # float64 for the running total. A float32 cumsum over minutes of audio loses
        # the low bits of every later term, and the average it yields drifts upward.
        total = np.cumsum(buf, dtype=np.float64)
        window = total[self._n - 1 :] - np.concatenate(([0.0], total[: -self._n]))
        self._tail = buf[-(self._n - 1) :]
        return (x - (window / self._n).astype(np.float32)).astype(np.float32)


class _Agc:
    """A trailing automatic gain, per sample, exactly the same in any chunking.

    Built the way `_DcBlock` is and for the same reason. The obvious AGC keeps a
    smoothed level and updates it once per BUFFER, which makes the gain a function of
    how the samples happened to be delivered — the identical defect that stood in the DC
    blocker until `test_chunking_changes_nothing` caught it, and one that is inaudible
    on a single buffer. A boxcar mean-square over a carried tail costs one cumulative
    sum, gives a gain for EVERY sample rather than one per buffer, and depends on
    nothing but the samples.

    A per-sample gain also removes the step a per-buffer gain puts at each boundary,
    which is a click ten times a second whenever the level is moving.

    **The attack is the window.** A signal that arrives suddenly is amplified by the old,
    higher gain until the average catches up — a quarter of a second, bounded by the clip
    in `_to_pcm`, which is what an overdriven receiver does anyway. A fast-attack /
    slow-release pair would fix that and cannot be written with one boxcar; it is not
    worth two, and a symmetric window is what an SSB operator would call a slow AGC."""

    def __init__(
        self,
        rate_hz: int,
        *,
        window_s: float = AGC_WINDOW_S,
        target_rms: float = AGC_TARGET_RMS,
    ) -> None:
        self._n = max(2, int(window_s * rate_hz))
        self._tail = np.zeros(self._n - 1, dtype=np.float32)
        self._target = float(target_rms)
        self._lo = float(10.0 ** (AGC_MIN_GAIN_DB / 20.0))
        self._hi = float(10.0 ** (AGC_MAX_GAIN_DB / 20.0))
        #: The RMS below which the input is treated as silence rather than as something
        #: to amplify. Without it an empty channel divides by nearly zero and the gain
        #: pins at its ceiling — which is where the ceiling would be doing the work
        #: instead of this.
        self._floor = self._target / self._hi

    def feed(self, x: np.ndarray) -> tuple[np.ndarray, float]:
        """The gained audio, and where the gain ended up, in dB."""
        if x.size == 0:
            return x, 0.0
        buf = np.concatenate([self._tail, x.astype(np.float32, copy=False)])
        # float64 for the running total, as in `_DcBlock`: a float32 cumsum of squares
        # over minutes of audio loses the low bits of every later term.
        total = np.cumsum(np.square(buf, dtype=np.float64))
        window = total[self._n - 1 :] - np.concatenate(([0.0], total[: -self._n]))
        self._tail = buf[-(self._n - 1) :]
        rms = np.sqrt(window / self._n)
        gain = np.clip(self._target / np.maximum(rms, self._floor), self._lo, self._hi)
        return (x * gain.astype(np.float32)), float(20.0 * np.log10(gain[-1]))


class _Mixer:
    """A complex exponential that keeps its phase across buffers, from a table.

    Continuity matters for the same reason the filter tails do, and more visibly: a
    mixer restarted at phase zero every buffer puts a step in the carrier ten times a
    second, which an FM discriminator turns into an impulse — a tick in the audio and
    a smear across every bin of a spectrum taken from the same samples.

    **The offset is SNAPPED to a whole division of the sample rate**, which is what
    turns this from the most expensive stage into a free one. `np.exp` over a quarter
    of a million complex128 samples measured 9.7 ms a frame, more than the whole rest
    of the chain; at `rate / M` the tone repeats every M samples, so one period is
    built once and the hot path is a slice of a tiled table. The snap is invisible
    downstream because the exact offset is arbitrary — it exists only to move the
    station off the RTL2832U's DC spike — but it must be REPORTED, since the radio is
    tuned to `station + offset` and every frequency the spectrum carries is relative
    to that."""

    def __init__(self, offset_hz: float, rate_hz: float) -> None:
        divisions = round(rate_hz / offset_hz) if offset_hz else 0
        self.offset_hz = rate_hz / divisions if divisions else 0.0
        self._period = abs(divisions)
        self._n = 0
        self._table: np.ndarray | None = None

    def _tiled(self, want: int) -> np.ndarray:
        """One period repeated far enough to slice `want` samples from any phase."""
        if self._table is None or self._table.size < want + self._period:
            k = np.arange(self._period, dtype=np.float64)
            one = np.exp(-2j * np.pi * k / self._period).astype(np.complex64)
            reps = -(-(want + self._period) // self._period)
            self._table = np.tile(one, reps)
        return self._table

    def feed(self, x: np.ndarray) -> np.ndarray:
        if self._period == 0:
            return x
        table = self._tiled(x.size)
        out = x * table[self._n : self._n + x.size]
        self._n = (self._n + x.size) % self._period
        return out


@dataclass(frozen=True, slots=True, eq=False)
class Audio:
    """One buffer's worth of sound, and the channel it came out of.

    `eq=False` for the reason `iq.Spectrum` gives: equality on a dataclass holding an
    ndarray answers element-wise, so `a == b` raises rather than returning False."""

    #: Mono int16 at `audio_rate_hz`. `.tobytes()` is what ffmpeg and direwolf read,
    #: and is byte-identical in format to what `rtl_fm` wrote on its stdout.
    pcm: np.ndarray
    #: The decimated complex IF, centred on the tuned frequency, at `if_rate_hz`, as
    #: it is BEFORE the channel filter — `view_half_hz` wide, not `channel_half_hz`.
    #:
    #: The width is the point. An FFT of this is what the tuning strip draws, and a
    #: picture no wider than the filter cannot show a noise floor to judge a signal
    #: against, an adjacent station to be confused by, or the part of a signal that
    #: falls outside the passband it shades. Worse, it makes the FILTER'S OWN SHAPE
    #: the picture: an empty channel came out as a centred hump 13.7 dB over its own
    #: edges, which is a station drawn out of nothing (see `VIEW_SHARE`).
    baseband: np.ndarray
    #: Loudest sample as a 0..1 fraction of full scale, the same quantity
    #: `listen._peak` measures — computed here because the samples are already in a
    #: numpy array, where `listen`'s struct.unpack + max over a Python generator is
    #: the single most expensive thing in that pump.
    #:
    #: **It is a MAX, and on FM that makes it a poor answer to "is this clipping".**
    #: A discriminator turns each burst of noise that momentarily overpowers the
    #: carrier into a full-scale impulse — the click every FM receiver makes on a weak
    #: signal — so one click in a buffer of 1600 samples pins this at 1.0 while the
    #: other 1599 are a perfectly good voice. Measured on air: NOAA weather at 17 dB
    #: SNR reads 1.0 on every buffer. Use `clipped` for that question.
    peak: float
    #: What fraction of the buffer actually hit the rail, 0..1. THIS is the clipping
    #: measure: a tenth of a percent is the impulse noise above, half is a chain whose
    #: gain is wrong.
    #:
    #: Measured AFTER the AGC, unlike `peak` and `rms`: the rail is where the audio
    #: ends up, not where the detector left it.
    clipped: float
    #: Root mean square of the buffer, 0..1 — how loud it really is, as against how
    #: loud its single worst sample was.
    rms: float
    #: What the AGC is doing, in dB, at the end of this buffer. Zero on FM, which has
    #: none.
    #:
    #: **`peak` and `rms` above are measured BEFORE it**, deliberately. They are the only
    #: honest answer this chain gives to "how strong is the signal", `listen-probe` reads
    #: them to decide whether anything is on the air at all, and an AGC that moved them
    #: would make a dead channel and a loud one report the same number. What the AGC
    #: changes is what the owner HEARS; this field is how much.
    gain_db: float = 0.0

    def tobytes(self) -> bytes:
        return self.pcm.tobytes()


class Demodulator:
    """One mode, one capture rate, one long-lived filter chain.

    Built once per tuning and fed every buffer the radio hands over. Rebuilt on a
    retune rather than adjusted, because the chain's state describes the signal it was
    tracking and carrying that across a frequency change is worse than a gap."""

    def __init__(
        self,
        mode: str,
        capture_rate_hz: int,
        *,
        audio_rate_hz: int = AUDIO_RATE,
        offset_hz: float = 0.0,
        bandwidth_hz: int | None = None,
    ) -> None:
        key = mode.lower()
        if key not in IF_RATE_HZ:
            raise DemodError(f"unknown mode {mode!r}")
        ladder = BANDWIDTH_HZ[key]
        low, high = BANDWIDTH_RANGE_HZ[key]
        if bandwidth_hz is None:
            bandwidth_hz = ladder[0]
        elif not low <= bandwidth_hz <= high:
            # Bounded rather than clamped: a clamped width would leave the radio
            # listening at something other than the number on screen, which is the one
            # failure this whole control exists to end. Naming the bounds makes the
            # refusal actionable for a caller with no terminal.
            raise DemodError(
                f"{key} filters run {low}-{high} Hz wide, not {bandwidth_hz}"
            )
        elif bandwidth_hz % BANDWIDTH_STEP_HZ:
            raise DemodError(
                f"a filter width must be a multiple of {BANDWIDTH_STEP_HZ} Hz, "
                f"and {bandwidth_hz} is not"
            )
        if_rate = IF_RATE_HZ[key]
        if capture_rate_hz % if_rate:
            raise DemodError(
                f"{key} needs a capture rate divisible by {if_rate} Hz, "
                f"and {capture_rate_hz} is not"
            )
        if if_rate % audio_rate_hz:
            raise DemodError(
                f"{key}'s {if_rate} Hz IF is not divisible by {audio_rate_hz} Hz audio"
            )
        self.mode = key
        self.capture_rate_hz = int(capture_rate_hz)
        self.if_rate_hz = if_rate
        self.audio_rate_hz = int(audio_rate_hz)
        #: The width the owner chose, as a FULL channel width — the number the control
        #: shows and the one every other field here is derived from.
        self.bandwidth_hz = int(bandwidth_hz)
        self.channel_half_hz = channel_half_for(key, self.bandwidth_hz)
        #: (low, high) offsets from the tuned frequency of what this mode actually
        #: hears — one-sided on SSB (C14). `channel_half_hz` above is the FILTER's
        #: half-width and cannot say it.
        self.passband_hz: tuple[float, float] = passband_for(key, self.bandwidth_hz)
        #: How far from the dial the TUNING PICTURE has to reach, from the mode's
        #: WIDEST filter rather than the one in force.
        #:
        #: **This is what keeps the point of the control visible.** `listen._tuning_frame`
        #: crops the strip to four times the reach it is given, so deriving it from the
        #: current passband would zoom the picture in every time the owner narrowed the
        #: filter — hiding the interfering station at the exact moment they narrowed the
        #: filter to reject it, and leaving the shaded box the same fraction of the
        #: picture at every setting, which makes the control look like it did nothing.
        #: Pinning it to the widest rung holds the picture still and lets the shaded box
        #: shrink inside it, which is the whole visual argument.
        self.crop_reach_hz = channel_half_for(key, high)
        #: The same thing as the two numbers a viewer needs: how wide to shade, and how
        #: far off the tuned frequency to centre the shading. Zero centre on every
        #: symmetric mode, which is why a client that ignores it draws what it always
        #: drew.
        self.passband_width_hz = self.passband_hz[1] - self.passband_hz[0]
        self.passband_centre_hz = (self.passband_hz[0] + self.passband_hz[1]) / 2.0
        #: What the FRONT END keeps flat, and therefore how wide the picture is. Never
        #: narrower than the channel: wide FM's 90 kHz is most of its 240 kHz IF
        #: already, so there is nothing to widen to and the front end is its own
        #: channel filter (`_build_channel` returns None for it).
        #: The rate the PICTURE is sampled at — the IF rate whenever that is wide
        #: enough to hold the channel and a margin around it, and a whole multiple of
        #: it when it is not (see `VIEW_MARGIN`).
        self.view_rate_hz = if_rate
        while (
            VIEW_SHARE * self.view_rate_hz < VIEW_MARGIN * self.channel_half_hz
            and self.view_rate_hz * 2 <= capture_rate_hz
            and capture_rate_hz % (self.view_rate_hz * 2) == 0
        ):
            self.view_rate_hz *= 2
        #: ...and how much of it the front end keeps flat.
        self.view_half_hz = max(self.channel_half_hz, VIEW_SHARE * self.view_rate_hz)
        deviation = FM_DEVIATION_HZ.get(key)
        self._gain = FM_HEADROOM * if_rate / (2.0 * deviation) if deviation else 1.0

        self._mixer = _Mixer(offset_hz, capture_rate_hz)
        #: The offset ACTUALLY used, after snapping to a whole division of the rate.
        #:
        #: **The radio is tuned `offset_hz` BELOW the station**, so a caller SUBTRACTS
        #: this from the frequency it wants to hear. The mixer shifts the spectrum DOWN
        #: by `offset_hz` (`_Mixer` multiplies by `exp(-j...)`), so what lands at DC is
        #: whatever sat `offset_hz` ABOVE the tuned centre.
        #:
        #: The sign is spelled out because getting it backwards is silent and total.
        #: This said "above" and `listen.py` implemented it, which put the station at
        #: -offset_hz where the mixer moved it to -2*offset_hz — 480 kHz from DC, past
        #: every filter in the chain. MEASURED ON AIR 2026-09-06: a carrier at
        #: +240 kHz reaches DC at +30.1 dB and one at -240 kHz reaches it at -35.1,
        #: sixty-five decibels down, so the station was not attenuated but GONE, and
        #: what the discriminator got was the empty spectrum 480 kHz above it. On the
        #: box, asking to hear 99.3 read 3.7 dB over the noise; asking for 99.3 minus
        #: 480 kHz read 21.8 dB with four times the audio.
        self.offset_hz = self._mixer.offset_hz
        # When the IF is wide enough to carry the picture — every mode but wide FM —
        # the front end serves both and its output IS the picture, for free. When it
        # is not, the front end serves the AUDIO only (so it need be flat across the
        # channel and no wider, which is what keeps it cheap) and `_view` makes the
        # wide picture separately out of the same buffer.
        wide = self.view_rate_hz != if_rate
        self._view_m = self.capture_rate_hz // self.view_rate_hz
        self._front = self._build_front(
            capture_rate_hz // if_rate,
            kept=self.channel_half_hz if wide else self.view_half_hz,
        )
        self._view = self._build_view() if wide else None
        self._channel = self._build_channel()
        self._back = self._build_back()
        # The discriminator differentiates phase, so it needs the sample BEFORE the
        # buffer it is given. One complex number, and without it there is a click per
        # buffer — the same defect as a missing filter tail, one stage further down.
        self._last = np.complex64(0)
        # AM only: an FM discriminator's output is already centred, so there is no
        # pedestal to remove and a high-pass would only cost a filter.
        self._dc = _DcBlock(DC_BLOCK_TAPS) if key == "am" else None
        # AM and SSB only, and see `AGC_WINDOW_S` for why not FM: their audio level IS
        # the RF level, so the same station is as loud as the propagation makes it.
        self._agc = _Agc(self.audio_rate_hz) if key in ("am", "usb", "lsb") else None

    # -- construction ---------------------------------------------------------------

    def _build_front(self, total: int, *, kept: float) -> list[_Fir]:
        """The decimation from the capture rate down to the IF. ANTI-ALIAS ONLY.

        Split into at most two stages, coarse first. The first stage only has to
        reject what would fold into the band the second one keeps, so its transition
        band is enormous and its taps few; the second does the sharp work at a tenth
        of the rate.

        Two things here are load-bearing and only one of them used to be right.

        **What is kept is `view_half_hz`, not the channel.** Filtering to the channel
        here left nothing outside it to look at, so the tuning view could not show a
        noise floor, an adjacent station, or how much of a signal falls outside the
        passband it shades — the three things it exists to show.

        **The stopband is where the fold starts, and that is all this has to say.**
        Everything above `out_rate - kept` lands inside the kept band on the way down,
        so those two edges ARE the specification; `lowpass` derives its own 6 dB point
        and its own length from them (C12). The midpoint arithmetic that used to live on
        this line is gone, along with the chance of writing the passband edge there —
        which is exactly what it said until 2026-09-06, for a response five decibels
        down at the edge of the band it claimed to keep (see `VIEW_SHARE`)."""
        stages: list[_Fir] = []
        rate = float(self.capture_rate_hz)
        for m in _split(total):
            out_rate = rate / m
            if m == 1:
                # Nothing folds, so there is nothing to reject (C24). The old code built
                # a filter anyway, whose stopband landed exactly on Nyquist and raised
                # from the constructor — unreachable from `listen.py` at today's rates
                # and a trap for the next one added.
                continue
            stages.append(
                _Fir(lowpass(kept, out_rate - kept, STOPBAND_DB, rate), m, complex_in=True)
            )
            rate = out_rate
        return stages

    def _build_view(self) -> np.ndarray:
        """The taps that make the wide picture, reversed for a dot product.

        Not an `_Fir`: this one keeps no state and is not fed everything. It runs over
        the tail of each buffer only (`VIEW_SAMPLES`), so there is no continuity across
        frames to preserve — an FFT window is its own beginning and end."""
        m = self.capture_rate_hz // self.view_rate_hz
        h = lowpass(
            self.view_half_hz,
            self.view_rate_hz - self.view_half_hz,
            STOPBAND_DB,
            float(self.capture_rate_hz),
        )
        self._view_m = m
        return np.ascontiguousarray(h[::-1])

    def _view_row(self, stream: np.ndarray) -> np.ndarray:
        """`VIEW_SAMPLES` of the wide picture, from the end of this buffer."""
        taps = self._view
        if taps is None:
            return stream
        need = VIEW_SAMPLES * self._view_m + taps.size
        chunk = stream[-need:] if stream.size > need else stream
        if chunk.size < taps.size + self._view_m:
            return np.zeros(0, dtype=np.complex64)
        window = sliding_window_view(chunk, taps.size)[:: self._view_m]
        return (window @ taps).astype(np.complex64)

    def _build_channel(self) -> _Fir | None:
        """The selectivity, at the IF rate: what the demodulator actually hears.

        Separate from the front end because the two want opposite things. The front
        end must leave the picture something to show; this must throw all of it away
        but the channel, because every hertz of noise it passes reaches a discriminator
        that is blind to amplitude and turns it into full-scale hiss.

        **Wide FM needs one, and a draft of this said it did not.** The claim was that
        90 kHz of channel in a 240 kHz IF is already most of Nyquist so the front end is
        its own channel filter — but the front end is specified by the FOLD, so its
        6 dB points sit at 240 and 120 kHz, and 120 kHz is not 90. It passes 113.7 kHz
        at about -5 dB. Deleting this filter on that reasoning cost 52 dB of LO-leakage
        suppression on wide FM (-63 dB to -11) and was caught by an adversarial review
        before it shipped."""
        kept = self.channel_half_hz
        # Against the IF's own usable half-band, since this runs at the IF rate — the
        # view can be wider and for wide FM is.
        room = 0.45 * self.if_rate_hz
        if kept >= 0.9 * room:
            return None
        # Wide enough to be affordable, narrow enough that the stopband is inside the
        # band this filter runs in — a transition that ran past it would be shaped by
        # the stage above instead, which is the confusion this split exists to end.
        stop = min(room, kept + max(0.5 * kept, 2_000.0))
        return _Fir(
            lowpass(kept, stop, STOPBAND_DB, float(self.if_rate_hz)), 1, complex_in=True
        )

    def _build_back(self) -> _Fir:
        """Demodulated audio to the output rate, with de-emphasis folded in.

        One filter doing two jobs. Both are linear and both run at the IF rate, so
        convolving the kernels costs one pass instead of two and the combined taps are
        no longer than the anti-alias filter alone would have been."""
        m = self.if_rate_hz // self.audio_rate_hz
        cutoff = AUDIO_CUTOFF_HZ[self.mode]
        # Same fold rule as the front end, at the output rate this time.
        stop = self.audio_rate_hz - cutoff
        if self.mode in ("usb", "lsb"):
            # SSB does its filtering on the COMPLEX baseband, before the sideband is
            # folded down: a real filter after the fold cannot tell the two sidebands
            # apart, which is the whole point of single sideband. A low-pass of half
            # the passband's width, shifted to its centre, is a complex bandpass that
            # keeps one side and rejects the other.
            # Derived from the chosen width, not from a fixed pair of edges: narrowing
            # SSB moves the OUTER edge in and leaves the inner one at `SSB_LOW_HZ`,
            # because the low edge is set by where the suppressed carrier sits and has
            # nothing to do with how much bandwidth the owner wants.
            low, high = sorted(map(abs, self.passband_hz))
            centre = (low + high) / 2.0
            half = (high - low) / 2.0
            # The transition is `SSB_LOW_HZ` wide, which is what puts the lower skirt on
            # the carrier rather than through it.
            base = lowpass(half, half + SSB_LOW_HZ, STOPBAND_DB, float(self.if_rate_hz))
            k = np.arange(base.size, dtype=np.float64) - (base.size - 1) / 2.0
            sign = 1.0 if self.mode == "usb" else -1.0
            shift = np.exp(1j * sign * 2.0 * np.pi * centre * k / self.if_rate_hz)
            return _Fir((base * shift).astype(np.complex128), m, complex_in=True)
        # Two edges, and `lowpass` places its own 6 dB point between them. This line
        # passed `cutoff` alone until 2026-09-06 — the same defect fixed in the front end
        # that day and left in the sibling function — so the 6 dB point sat on the
        # PASSBAND EDGE and the response had been sagging since DC. The honest way to
        # state the cost is as DEVIATION FROM THE INTENDED SHAPE: FM is meant to slope
        # (de-emphasis), so of the -6.6 dB measured at 3 kHz, 4.8 belonged there and 1.86
        # was this. AM is meant to be FLAT and was 1.93 dB down at 3 kHz with nothing to
        # blame. Either way it is the consonant band, heard as muffled speech. C12's
        # signature is what makes writing it that way impossible now.
        h = lowpass(cutoff, stop, STOPBAND_DB, float(self.if_rate_hz))
        if self.mode in ("fm", "nfm", "wbfm"):
            h = np.convolve(h, deemphasis(self.if_rate_hz))
            h = h / h.sum()
        return _Fir(h, m, complex_in=False)

    # -- the hot path ---------------------------------------------------------------

    def feed(self, samples: Any) -> Audio:
        """One capture buffer in, one buffer of audio and its baseband out.

        The buffer may be any length, including one too short to produce a single
        output sample: the chain keeps what it cannot use yet, so the caller is free
        to hand over whatever the radio gave it rather than blocking for a round
        number."""
        iq = np.asarray(samples)
        if iq.dtype != np.complex64:
            iq = iq.astype(np.complex64)
        stream = self._mixer.feed(iq)
        # The wide picture, when there is one, comes off the MIXER output — before the
        # front end has narrowed the band to what the audio needs.
        wide = self._view_row(stream) if self._view is not None else None
        for stage in self._front:
            stream = stage.feed(stream)
        # ...and when there is not, the front end's own output is the picture, which is
        # the whole economy of this path: one filter, two readings.
        view = stream if wide is None else wide
        channel = self._channel.feed(stream) if self._channel is not None else stream
        pcm, peak, clipped, rms, gain_db = self._to_pcm(channel)
        return Audio(
            pcm=pcm, baseband=view, peak=peak, clipped=clipped, rms=rms, gain_db=gain_db
        )

    def _to_pcm(self, baseband: np.ndarray) -> tuple[np.ndarray, float, float, float, float]:
        if baseband.size == 0:
            return np.zeros(0, dtype=np.int16), 0.0, 0.0, 0.0, 0.0
        audio = self._detect(baseband)
        audio = self._back.feed(audio)
        if np.iscomplexobj(audio):
            # SSB: the complex bandpass kept one sideband, so folding to real now
            # cannot bring the other one back. The 2x recovers the amplitude that
            # taking one half of a conjugate pair costs.
            audio = 2.0 * audio.real
        if self._dc is not None:
            audio = self._dc.feed(audio)
        if audio.size == 0:
            return np.zeros(0, dtype=np.int16), 0.0, 0.0, 0.0, 0.0
        # MEASURED HERE, before the AGC. `peak` and `rms` are the only honest answer this
        # chain gives to "how strong is the signal", and `listen-probe` reads them to
        # decide whether anything is on the air; taking them after a gain that aims at a
        # fixed level would make a dead channel and a loud one report the same number.
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        gain_db = 0.0
        if self._agc is not None:
            audio, gain_db = self._agc.feed(audio)
        # Clipped AFTER the gain, and it is the CLIP that stays: an AGC bounded at
        # `AGC_MAX_GAIN_DB` still meets signals it cannot fit, and clipping is what a
        # real receiver does then — audible as overdrive rather than invisible. FM has no
        # AGC at all (`AGC_WINDOW_S`), so for it this is exactly what it always was.
        clipped = float(np.count_nonzero(np.abs(audio) >= 1.0)) / float(audio.size)
        return (
            np.clip(audio * 32767.0, -32768.0, 32767.0).astype(np.int16),
            peak,
            clipped,
            rms,
            gain_db,
        )

    def _detect(self, baseband: np.ndarray) -> np.ndarray:
        """Complex channel to a real (or, for SSB, still-complex) audio signal."""
        if self.mode in ("fm", "nfm", "wbfm"):
            # The polar discriminator: the phase ADVANCE between consecutive samples
            # is instantaneous frequency, and for FM that is the modulation. Computed
            # as the angle of `x[n] * conj(x[n-1])` rather than by differencing two
            # `angle` calls, which is both half the arctangents and immune to the 2*pi
            # wrap that differencing has to unwrap by hand.
            prev = np.empty(baseband.size, dtype=np.complex64)
            prev[0] = self._last
            prev[1:] = baseband[:-1]
            self._last = baseband[-1]
            # Typed explicitly: `np.conj` widens to an unknown-dtype array, and the
            # `.imag`/`.real` below then have no attribute to resolve against.
            product: np.ndarray = baseband * np.conj(prev)
            # Scaled so full deviation is full scale. Peak advance per sample is
            # 2*pi*dev/rate radians, so dividing by pi puts a deviation of half the
            # IF's Nyquist at unity — which for a channel filtered to `channel_half`
            # is the loudest signal that can legitimately be in there.
            return np.arctan2(product.imag, product.real) * np.float32(self._gain / np.pi)
        if self.mode == "am":
            # Just the envelope. The carrier it rides on is DC, and `_DcBlock` takes it
            # off at the AUDIO rate — after the decimation rather than before it, where
            # the same corner frequency costs a fifteenth of the samples.
            return np.abs(baseband).astype(np.float32)
        # SSB: nothing to detect. The sideband is already at audio frequencies
        # relative to the (suppressed) carrier the mixer put at DC, so the complex
        # bandpass in the back end IS the demodulator.
        return baseband
