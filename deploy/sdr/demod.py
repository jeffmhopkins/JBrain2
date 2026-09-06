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

#: The channel a narrow mode keeps out of the IF, as a half-width. This is the
#: passband the tuning view shades: what the demodulator actually hears.
CHANNEL_HALF_HZ: dict[str, float] = {
    "fm": 8_000.0,
    "nfm": 8_000.0,
    "am": 8_000.0,
    "usb": 3_400.0,
    "lsb": 3_400.0,
    "wbfm": 90_000.0,
}

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

#: How long a window the AM carrier is averaged over, in AUDIO samples. The corner
#: is roughly `audio_rate / length` — 31 Hz at 512 and 16 kHz — which is well below
#: anything a voice channel carries and well above the drift it exists to remove.
DC_BLOCK_TAPS = 512

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

#: Where an SSB passband sits relative to the suppressed carrier. 300–3400 Hz is the
#: telephony band every SSB radio is built around; the filter is designed as a
#: low-pass of half that width and shifted to the middle of it.
SSB_LOW_HZ = 300.0
SSB_HIGH_HZ = 3_400.0

#: Roughly how many taps a windowed-sinc needs for a given transition width, as a
#: fraction of the sample rate. Hamming's rule of thumb is 3.3/Δf for ~53 dB of
#: stopband; 4.0 buys margin, and taps are cheap at these decimation factors.
_TAP_RULE = 4.0

#: Never fewer than this, whatever the rule says. A very wide transition can ask for
#: nine taps, and a nine-tap low-pass has a passband that is not flat.
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


def lowpass(cutoff_hz: float, rate_hz: float, taps: int) -> np.ndarray:
    """A windowed-sinc low-pass: odd length, linear phase, unity gain at DC.

    Odd length so the filter is symmetric about a whole sample and its delay is an
    integer — an even-length linear-phase filter delays by half a sample, which is
    harmless alone and becomes a half-sample of skew when two chains are compared.

    Normalised by its own sum rather than analytically: the truncation and the window
    both cost a little DC gain, and a filter that quietly attenuates by 0.4 dB per
    stage is a level error that compounds across the chain and shows up as a level
    meter that disagrees with the spectrum."""
    if cutoff_hz <= 0 or cutoff_hz >= rate_hz / 2:
        raise DemodError(f"cutoff {cutoff_hz} Hz is not inside 0..{rate_hz / 2} Hz")
    n = int(taps) | 1
    k = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
    # np.sinc is the NORMALISED sinc — sin(pi x)/(pi x) — so the argument is in
    # cycles, not radians. Passing 2*pi*fc/fs here is a classic and silent error: the
    # filter comes out with a cutoff 2*pi times too high, which at these ratios is
    # simply no filter at all.
    h = 2.0 * (cutoff_hz / rate_hz) * np.sinc(2.0 * (cutoff_hz / rate_hz) * k)
    h *= np.hamming(n)
    return h / h.sum()


def _taps_for(transition_hz: float, rate_hz: float) -> int:
    if transition_hz <= 0:
        raise DemodError("a filter needs a transition band wider than zero")
    return max(_MIN_TAPS, int(_TAP_RULE * rate_hz / transition_hz) | 1)


def deemphasis(rate_hz: float, tau_s: float = DEEMPHASIS_S) -> np.ndarray:
    """The de-emphasis one-pole, as an FIR of its own impulse response.

    A one-pole IIR is three lines and cannot be vectorised — each output needs the
    one before it, so it is a Python loop over every sample at the IF rate. Its
    impulse response is `(1-a) * a**n`, which at 48 kHz and 75 µs decays below
    float32's resolution inside fifty taps, so the FIR is not an approximation in any
    sense the arithmetic can tell. It also CONVOLVES with the anti-alias filter it
    sits next to, which is how both end up costing one pass instead of two."""
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

    @property
    def delay(self) -> int:
        """Group delay in OUTPUT samples — what this stage costs in latency."""
        return (self._taps.size - 1) // 2 // self._m

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
    """A trailing moving average, subtracted: a high-pass with a corner at `rate / n`.

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
    clipped: float
    #: Root mean square of the buffer, 0..1 — how loud it really is, as against how
    #: loud its single worst sample was.
    rms: float

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
    ) -> None:
        key = mode.lower()
        if key not in IF_RATE_HZ:
            raise DemodError(f"unknown mode {mode!r}")
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
        self.channel_half_hz = CHANNEL_HALF_HZ[key]
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
        #: The radio is tuned `offset_hz` above the station, so this is what a caller
        #: adds to the tuned frequency and subtracts from every spectrum centre.
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

        **The cutoff is the MIDDLE of the transition band, not its start.** A
        windowed sinc's `cutoff_hz` is where it is 6 dB down, so asking for a cutoff at
        the passband edge asks for a filter that is already half gone there and has been
        sagging since DC. Placing it halfway between the passband edge and the stopband
        edge is what buys a passband that is actually flat — the same taps, a response
        within a tenth of a decibel across the band instead of five decibels down at the
        edge of it (see `VIEW_SHARE` for what that cost on air)."""
        stages: list[_Fir] = []
        rate = float(self.capture_rate_hz)
        for m in _split(total):
            out_rate = rate / m
            # Everything above `out_rate - kept` folds into the kept band on the way
            # down. That, not the cutoff, is what sets the filter's length.
            stop = out_rate - kept
            taps = _taps_for(max(stop - kept, 1.0), rate)
            stages.append(_Fir(lowpass((kept + stop) / 2.0, rate, taps), m, complex_in=True))
            rate = out_rate
        return stages

    def _build_view(self) -> np.ndarray:
        """The taps that make the wide picture, reversed for a dot product.

        Not an `_Fir`: this one keeps no state and is not fed everything. It runs over
        the tail of each buffer only (`VIEW_SAMPLES`), so there is no continuity across
        frames to preserve — an FFT window is its own beginning and end."""
        m = self.capture_rate_hz // self.view_rate_hz
        stop = self.view_rate_hz - self.view_half_hz
        taps = _taps_for(max(stop - self.view_half_hz, 1.0), float(self.capture_rate_hz))
        h = lowpass((self.view_half_hz + stop) / 2.0, float(self.capture_rate_hz), taps)
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

        None for wide FM, which has no room for one: 90 kHz of channel in a 240 kHz IF
        is already most of Nyquist, so the front end is its own channel filter there and
        a second pass would buy a fraction of a decibel for a hundred and sixty taps."""
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
        taps = _taps_for(stop - kept, float(self.if_rate_hz))
        return _Fir(lowpass((kept + stop) / 2.0, float(self.if_rate_hz), taps), 1, complex_in=True)

    def _build_back(self) -> _Fir:
        """Demodulated audio to the output rate, with de-emphasis folded in.

        One filter doing two jobs. Both are linear and both run at the IF rate, so
        convolving the kernels costs one pass instead of two and the combined taps are
        no longer than the anti-alias filter alone would have been."""
        m = self.if_rate_hz // self.audio_rate_hz
        cutoff = AUDIO_CUTOFF_HZ[self.mode]
        # Same fold rule as the front end, at the output rate this time.
        stop = self.audio_rate_hz - cutoff
        taps = _taps_for(max(stop - cutoff, 1.0), self.if_rate_hz)
        if self.mode in ("usb", "lsb"):
            # SSB does its filtering on the COMPLEX baseband, before the sideband is
            # folded down: a real filter after the fold cannot tell the two sidebands
            # apart, which is the whole point of single sideband. A low-pass of half
            # the passband's width, shifted to its centre, is a complex bandpass that
            # keeps one side and rejects the other.
            centre = (SSB_LOW_HZ + SSB_HIGH_HZ) / 2.0
            half = (SSB_HIGH_HZ - SSB_LOW_HZ) / 2.0
            base = lowpass(half, self.if_rate_hz, _taps_for(SSB_LOW_HZ, self.if_rate_hz))
            k = np.arange(base.size, dtype=np.float64) - (base.size - 1) / 2.0
            sign = 1.0 if self.mode == "usb" else -1.0
            shift = np.exp(1j * sign * 2.0 * np.pi * centre * k / self.if_rate_hz)
            return _Fir((base * shift).astype(np.complex128), m, complex_in=True)
        h = lowpass(cutoff, self.if_rate_hz, taps)
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
        pcm, peak, clipped, rms = self._to_pcm(channel)
        return Audio(pcm=pcm, baseband=view, peak=peak, clipped=clipped, rms=rms)

    def _to_pcm(self, baseband: np.ndarray) -> tuple[np.ndarray, float, float, float]:
        if baseband.size == 0:
            return np.zeros(0, dtype=np.int16), 0.0, 0.0, 0.0
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
            return np.zeros(0, dtype=np.int16), 0.0, 0.0, 0.0
        magnitude = np.abs(audio)
        peak = float(np.max(magnitude))
        clipped = float(np.count_nonzero(magnitude >= 1.0)) / float(audio.size)
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        # Clipped, not scaled to fit. An automatic gain that rescales per buffer makes
        # a quiet channel as loud as a strong one and destroys the only honest thing
        # the level meter reports; clipping is what a real receiver does when it is
        # overdriven, and it is audible as overdrive rather than invisible.
        return (
            np.clip(audio * 32767.0, -32768.0, 32767.0).astype(np.int16),
            peak,
            clipped,
            rms,
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
