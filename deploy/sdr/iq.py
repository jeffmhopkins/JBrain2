"""Raw I/Q in, one waterfall row out: the FFT this sidecar now does for itself.

This is the whole spectrum engine, and it deliberately contains **no radio**. No
SoapySDR import, no subprocess, no device handle — a buffer of complex samples goes
in and a `Spectrum` comes out, which is what makes the physics testable on a machine
with no dongle attached (docs/plans/SDR_IQ_SPECTRUM_PLAN.md §7, F2). Everything that
can fail because hardware is absent lives in `radio.py`; everything that can be wrong
about *arithmetic* lives here, where a synthetic tone can prove it.

**The input is a numpy buffer, not a byte pipe.** The rejected design piped
`rtl_sdr`'s stdout in and paid for a u8 -> complex64 lookup table on every frame.
SoapySDR's `setupStream` offers CF32/CS16/CS8 and does that conversion inside its own
C++ loop, so the LUT stage has no equivalent here (§6.16) — CF32 arrives ready and
CS8 is a scale and a view.

**Welch across EVERY non-overlapping segment, not a sample of them.** Measured: 4096
bins over a 100 ms frame of 2.4 MS/s is ~8.5 ms, ~10% of one core at 10 fps (§3).
Averaging a subset would buy back a few milliseconds by ignoring samples the radio
did hand us — which is the looking-away problem the fast tier exists to remove, moved
from the USB boundary into this file. So: maximum quality, and the cost is affordable.
`np.fft` releases the GIL, which is what lets this run beside `http.server`.

**One batched FFT over a 2-D array**, never a Python loop over segments: identical
arithmetic, several times slower, and the interpreter holds the GIL between calls.

**`N` is a parameter and is NOT a power of two.** `bin_hz` is `rate / N` and it has to
divide exactly or the frame lies about where its bins are (§6.13): 2.4 MS/s at N=4096
is 585.9375 Hz, and rounding that to 586 puts the top of the frame 256 Hz out with
nothing downstream able to tell. N=4000 at the same rate is 600 Hz exactly, and
numpy's pocketfft is fast for any 5-smooth size (4000 = 2^5 * 5^3), so the power of
two was never buying anything worth that.

**dBFS, with the window's coherent gain divided out**, so a full-scale tone reads
0 dBFS whatever window is in front of it. This is the first true RF power figure in
the system: every other "level" here is `max(abs(sample))` over demodulated PCM,
which is audio loudness (§2.4).

**Nothing leaves here as `np.float32`.** `json.dumps` accepts `np.float64` — it
subclasses `float` — and refuses `np.float32`, so the bug is dtype-dependent and
stays invisible until it reaches the wire (§6.6). `db` is float64 and goes out
through `.tolist()`, which yields native floats.

`as_dict` matches `listen.Frame.as_dict` key for key, deliberately: F6 swaps the
engine under an unchanged wire contract, and a key added here would be a schema
change nobody asked for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

# The floor a silent bin lands on. log10(0) is -inf, `json.dumps` writes that as
# `-Infinity`, and that is not JSON — `JSON.parse` in the PWA refuses the frame. An
# exactly-zero bin is not hypothetical: a muted branch or a buffer of zeros produces
# a whole row of them. A floor no real reading can reach costs nothing.
DB_FLOOR = -200.0
_POWER_FLOOR = 10.0 ** (DB_FLOOR / 10.0)


def bin_width_hz(rate_hz: int, n: int) -> int | float:
    """`rate / n` — an int when the division is exact, a float when it is not.

    Never rounded. `Frame.bin_hz` is typed `int` downstream and the PWA's `sameBand`
    compares it exactly, so a rounded 585.9375 -> 586 is both a mislabelled frame and
    a value that flaps against the one the next component computes. Choosing a rate
    and an N that divide is the caller's job (§6.13); this returns a float rather than
    quietly absorbing the error, so an inexact pairing is visible at the type instead
    of being discovered on a waterfall that is 256 Hz out at the top."""
    if n <= 0:
        raise ValueError("n must be positive")
    if rate_hz % n == 0:
        return rate_hz // n
    return rate_hz / n


def as_complex64(samples: Any) -> np.ndarray:
    """The stream's buffer as complex64, converting CS8 if that is what arrived.

    CF32 is the primary path and costs nothing — SoapyRTLSDR's own C++ loop already
    turned the dongle's u8 into floats inside `readStream`. CS8 is accepted because
    `setupStream` offers it and it halves the memory traffic on the USB side; it
    arrives as interleaved int8, which is a scale and a view away from complex64.

    The scale is 1/128, not 1/127. int8 runs -128..127, so dividing by 127 lets the
    most negative code exceed unity and read as *positive* dBFS — a full-scale
    reference that can be overshot is not a reference. The cost is that a true
    full-scale tone reads -0.07 dBFS, which is a smaller lie than a scale with no
    top."""
    arr = np.asarray(samples)
    if arr.dtype == np.complex64:
        return arr
    if arr.dtype == np.int8:
        if arr.size % 2:
            raise ValueError("a CS8 buffer must hold whole I/Q pairs")
        # Multiplied by the reciprocal rather than divided, and by a float32 rather
        # than a Python float: 1/128 is a power of two so the two are bit-identical,
        # and measured over a 100 ms frame the division costs 1.8 ms against 0.2 ms.
        scaled = arr.astype(np.float32) * np.float32(1.0 / 128.0)
        return scaled.view(np.complex64)
    if np.iscomplexobj(arr):
        return arr.astype(np.complex64)
    raise TypeError(f"expected complex or CS8 (int8) samples, got {arr.dtype}")


@dataclass(frozen=True, slots=True, eq=False)
class Spectrum:
    """One waterfall row: every bin of one frame, in dBFS, low frequency first.

    Self-describing for the same reason `listen.Frame` is — `start_hz` and `bin_hz`
    ride on the row, so a retune needs no protocol event and a client that draws what
    each row says is already correct across one.

    `db` stays a numpy array rather than a list: F6 has percentile work to do on it,
    and converting once at the wire is cheaper than converting on every hop. Use
    `db_list()` or `as_dict()` to leave numpy behind.

    `eq=False` because equality on a dataclass holding an ndarray compares
    element-wise and returns an array, so `a == b` would raise on truth-testing
    rather than answer."""

    at: float
    # `int | float` for the same reason `bin_hz` is: exact where the rate and N
    # divide, honest where they do not, rather than rounded into agreement.
    start_hz: int | float
    bin_hz: int | float
    db: np.ndarray
    # How many segments the Welch average actually covered. The honest denominator:
    # a short frame is averaged over fewer segments and is noisier, and that is worth
    # being able to see rather than inferring from the sample count.
    segments: int

    @property
    def bins(self) -> int:
        return int(self.db.size)

    @property
    def stop_hz(self) -> int | float:
        # Derived from the array, never carried separately, so that
        # `start_hz + i * bin_hz` addresses bin `i` exactly.
        return self.start_hz + self.bins * self.bin_hz

    def db_list(self) -> list[float]:
        """The row as native Python floats. `.tolist()` is the json fix (§6.6)."""
        return self.db.tolist()

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 3),
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "bin_hz": self.bin_hz,
            "bins": self.bins,
            # One decimal, for the reason `listen.Frame` gives: the second is a tenth
            # of a dB, well under the noise, and costs a character per bin on every
            # frame of every viewer's stream. Rounded IN NUMPY, though — the
            # equivalent list comprehension measures 1.41 ms of GIL-held Python per
            # subscriber per frame (§5), which at 1 fps was invisible and at 10 fps
            # is not.
            "db": np.round(self.db, 1).tolist(),
        }


class Spectrometer:
    """Windowed, Welch-averaged FFT for one (N, rate) pairing.

    Built once and reused across frames. The window and its normalisation are pure
    functions of N that have no business being recomputed ten times a second, and
    numpy caches its pocketfft plan per transform size, so a long-lived instance is
    also what keeps that cache warm."""

    def __init__(self, n: int, rate_hz: int) -> None:
        if n < 2:
            raise ValueError("n must be at least 2")
        self.n = n
        self.rate_hz = rate_hz
        self.bin_hz = bin_width_hz(rate_hz, n)
        # PERIODIC Hann (denominator n), not numpy's symmetric `np.hanning`
        # (denominator n-1). The DFT treats the segment as one period of an infinite
        # signal, and the symmetric window repeats its endpoint under that extension,
        # which is a discontinuity the window exists to remove. The periodic form has
        # the property the tests pin: its own transform is exactly three taps
        # (-1/4, 1/2, -1/4), so a bin-centred tone leaks into exactly two neighbours
        # at -6.02 dB and nowhere else.
        k = np.arange(n, dtype=np.float64)
        self.window = 0.5 - 0.5 * np.cos(2.0 * np.pi * k / n)
        # Coherent-gain correction. An unwindowed bin-centred tone of amplitude A
        # peaks at A*n; windowed it peaks at A*sum(w), which for Hann is A*n/2 — the
        # window's 6.02 dB of coherent loss. Normalising by sum(w)^2 rather than n^2
        # divides that out, so full scale is 0 dBFS with any window and dBFS means the
        # ADC's full scale rather than "full scale, as attenuated by whatever window
        # this build happened to use".
        self._power_scale = 1.0 / float(self.window.sum()) ** 2

    def start_hz(self, center_hz: int) -> int | float:
        """Frequency of bin 0 after the shift — the low edge of the row.

        Computed from the REQUESTED integers, never from a rate read back off the
        hardware: librtlsdr quantises to the 28.8 MHz divider, and a ±1 Hz flap in a
        derived `start_hz` re-blanks the PWA's waterfall and re-freezes its colour
        scale on every frame (§6.8). `n // 2` rather than `n / 2` because that is
        where `fftshift` puts DC for odd n too."""
        return center_hz - (self.n // 2) * self.bin_hz

    def frame(self, samples: Any, center_hz: int, at: float | None = None) -> Spectrum:
        """One row from one frame's samples, low frequency first, in dBFS.

        A partial trailing segment is DROPPED, not zero-padded. Padding would
        manufacture a spectrum for time the radio never sampled and pull every bin's
        power down by the padded fraction — an invented reading is worse than a
        marginally shorter average."""
        iq = as_complex64(samples)
        segments = int(iq.size) // self.n
        if segments == 0:
            raise ValueError(
                f"need at least one whole segment of {self.n} samples, got {iq.size}"
            )
        seg2d = iq[: segments * self.n].reshape(segments, self.n) * self.window
        spec = np.fft.fft(seg2d, axis=1)
        # `real**2 + imag**2` rather than `abs(spec)**2`: the same number without the
        # square root that the square would immediately undo.
        power = spec.real**2 + spec.imag**2
        mean = power.mean(axis=0)
        mean *= self._power_scale
        np.maximum(mean, _POWER_FLOOR, out=mean)
        db = np.fft.fftshift(10.0 * np.log10(mean))
        return Spectrum(
            at=time.time() if at is None else at,
            start_hz=self.start_hz(center_hz),
            bin_hz=self.bin_hz,
            db=db,
            segments=segments,
        )
