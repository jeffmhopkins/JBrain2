"""The sdr sidecar's spectrum engine — the physics, without a radio.

`deploy/sdr/` is not an installed package, so it is loaded by path here, exactly as
`test_sdr_listen.py` loads the session. What these cover is arithmetic that no
hardware can help with and no hardware is needed for: a synthetic tone of known
frequency and known amplitude must land in the bin its frequency names, at the level
its amplitude names, and the frame that carries it must survive `json.dumps`.

The four traps the plan calls out by name are each pinned by a test, because every one
of them is silent in production: a rounded `bin_hz` (§6.13), an `np.float32` that
`json.dumps` refuses while `np.float64` passes (§6.6), a zero-padded trailing segment
that invents a spectrum, and a `-Infinity` from a silent bin.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load():
    # Same loader as test_sdr_listen.py. `iq.py` imports nothing from its siblings —
    # numpy is its only dependency — but the directory goes on sys.path anyway so
    # that one convention covers every sidecar module.
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location("sdr_iq", DEPLOY / "sdr/iq.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_iq"] = module
    spec.loader.exec_module(module)
    return module


iq = _load()

# The fast tier's real configuration (§6.13): 2.4 MS/s at N=4000 is 600 Hz exactly.
RATE = 2_400_000
N = 4000
CENTER = 145_000_000


def _tone(samples: int, offset_hz: float, amplitude: float = 1.0) -> np.ndarray:
    """A complex exponential at `offset_hz` from the centre, as the stream sees it."""
    k = np.arange(samples, dtype=np.float64)
    phase = 2.0j * np.pi * offset_hz * k / RATE
    return (amplitude * np.exp(phase)).astype(np.complex64)


def _bin_of(frame, hz: float) -> int:
    """The bin index `hz` claims, computed only from what the frame declares."""
    return round((hz - frame.start_hz) / frame.bin_hz)


def test_a_tone_lands_in_the_bin_its_frequency_names() -> None:
    """The frame's own `start_hz`/`bin_hz` must address it — that is the contract a
    client draws from, so an index computed any other way would prove nothing."""
    spectro = iq.Spectrometer(N, RATE)
    offset = 100 * 600  # 60 kHz above centre: bin-centred by construction
    frame = spectro.frame(_tone(N * 8, offset), CENTER)

    assert int(np.argmax(frame.db)) == _bin_of(frame, CENTER + offset)

    below = spectro.frame(_tone(N * 8, -offset), CENTER)
    assert int(np.argmax(below.db)) == _bin_of(below, CENTER - offset)


def test_a_full_scale_tone_reads_zero_dbfs() -> None:
    """0 dBFS, not -6.02, and that is the coherent-gain correction doing its job.

    A Hann window multiplies a full-scale tone by an average of 0.5, so an FFT
    normalised by N would report every full-scale signal 6.02 dB low — a number that
    is about the window rather than about the radio. Normalising by `sum(w)` instead
    divides that out, and dBFS goes back to meaning the ADC's full scale. Halving the
    amplitude must then move the reading by exactly 6.02 dB."""
    spectro = iq.Spectrometer(N, RATE)
    offset = 100 * 600

    full = spectro.frame(_tone(N * 8, offset), CENTER)
    assert full.db.max() == pytest.approx(0.0, abs=0.01)

    half = spectro.frame(_tone(N * 8, offset, amplitude=0.5), CENTER)
    assert half.db.max() == pytest.approx(-6.0206, abs=0.01)

    # Hann's transform is three taps, so a bin-centred tone puts exactly half the
    # amplitude in each neighbour and nothing anywhere else.
    peak = int(np.argmax(full.db))
    assert full.db[peak - 1] == pytest.approx(-6.0206, abs=0.01)
    assert full.db[peak + 1] == pytest.approx(-6.0206, abs=0.01)


def test_two_tones_do_not_smear_into_each_other() -> None:
    """Two half-scale carriers 252 kHz apart stay two carriers: each reads -6 dBFS in
    its own bin, and the space between them stays at the floor rather than filling in
    with the skirts of either. Both offsets are whole multiples of the 600 Hz bin, so
    any spreading seen here is the window's and not a tone straddling two bins."""
    spectro = iq.Spectrometer(N, RATE)
    low_hz, high_hz = CENTER - 150_000, CENTER + 102_000
    samples = _tone(N * 8, -150_000, 0.5) + _tone(N * 8, 102_000, 0.5)
    frame = spectro.frame(samples, CENTER)

    low_bin, high_bin = _bin_of(frame, low_hz), _bin_of(frame, high_hz)
    assert frame.db[low_bin] == pytest.approx(-6.0206, abs=0.01)
    assert frame.db[high_bin] == pytest.approx(-6.0206, abs=0.01)

    # Everything strictly between the two, minus each tone's two Hann neighbours.
    between = frame.db[low_bin + 2 : high_bin - 1]
    assert between.max() < -60.0, "a Hann window must not bridge 252 kHz"


def test_the_noise_floor_sits_where_theory_says() -> None:
    """White noise of unit power per sample lands at `10*log10(sum(w^2)/sum(w)^2)`.

    For a periodic Hann that is exactly `10*log10(1.5/N)` — the window's 1.5-bin
    noise-equivalent bandwidth against the coherent gain the tone test pins. It is
    the processing gain the plan counts on in §4 (~36 dB at N=4096, less ~1.8 for the
    window), so getting it wrong would mislabel every HF reading. Welch over 64
    segments leaves a per-bin spread of ~0.54 dB, which the median across 4000 bins
    flattens to well inside the tolerance."""
    spectro = iq.Spectrometer(N, RATE)
    rng = np.random.default_rng(20260904)
    size = N * 64
    noise = (rng.normal(size=size) + 1j * rng.normal(size=size)) / math.sqrt(2.0)

    frame = spectro.frame(noise.astype(np.complex64), CENTER)

    assert frame.segments == 64
    expected = 10.0 * math.log10(1.5 / N)
    assert float(np.median(frame.db)) == pytest.approx(expected, abs=0.2)


def test_the_frame_geometry_is_exactly_what_was_asked_for() -> None:
    """N bins, `bin_hz * N == rate`, and a span centred on the tuned frequency."""
    spectro = iq.Spectrometer(N, RATE)
    frame = spectro.frame(_tone(N * 4, 0.0), CENTER)

    assert frame.bins == N
    assert frame.bin_hz * N == RATE
    assert frame.start_hz == CENTER - RATE // 2
    assert frame.stop_hz == CENTER + RATE // 2
    assert frame.stop_hz - frame.start_hz == RATE


def test_the_frame_is_ordered_low_to_high() -> None:
    """`fftshift` is what makes bin 0 the low edge; without it the row would start at
    DC, run to +Nyquist and wrap, and a waterfall would be drawn in two halves the
    wrong way round."""
    spectro = iq.Spectrometer(N, RATE)
    order = [-900_000, -300_000, 300_000, 900_000]
    peaks = [int(np.argmax(spectro.frame(_tone(N * 4, hz), CENTER).db)) for hz in order]

    assert peaks == sorted(peaks)


def test_n_need_not_be_a_power_of_two() -> None:
    """4000 is 2^5 * 5^3 — 5-smooth, so pocketfft is fast on it — and 2.4 MS/s
    divided by it is 600 Hz with nothing left over. 4096 at the same rate is
    585.9375, which is why the power of two is the one that has to go (§6.13)."""
    assert iq.bin_width_hz(RATE, 4000) == 600
    assert isinstance(iq.bin_width_hz(RATE, 4000), int)

    inexact = iq.bin_width_hz(RATE, 4096)
    assert inexact == 585.9375
    assert isinstance(inexact, float), "an inexact bin width must not round to an int"

    # The other rates the plan settles on, all exact at N=4000.
    assert iq.bin_width_hz(2_048_000, 4000) == 512
    assert iq.bin_width_hz(1_024_000, 4000) == 256


def test_the_frame_survives_json_dumps() -> None:
    """The np.float32 trap, made explicit.

    `json.dumps` accepts `np.float64` because it subclasses `float`, and refuses
    `np.float32` because nothing does — so a serialiser that works today breaks the
    day someone halves a dtype to save memory, with no test in between. `.tolist()`
    is the fix, and native floats are what it must actually produce."""
    spectro = iq.Spectrometer(N, RATE)
    frame = spectro.frame(_tone(N * 4, 60_000), CENTER)

    payload = json.dumps(frame.as_dict())
    assert len(json.loads(payload)["db"]) == N
    assert all(type(v) is float for v in frame.as_dict()["db"])
    assert all(type(v) is float for v in frame.db_list())

    # The trap itself, so the reason this test exists cannot be argued away.
    json.dumps(float(np.float64(1.0)))
    with pytest.raises(TypeError):
        json.dumps({"db": [np.float32(1.0)]})


def test_a_silent_bin_floors_rather_than_serialising_as_infinity() -> None:
    """`json.dumps` writes -inf as `-Infinity`, which is not JSON and which the PWA's
    `JSON.parse` rejects — so an all-zero buffer would take the whole stream down
    rather than draw a quiet band."""
    spectro = iq.Spectrometer(N, RATE)
    frame = spectro.frame(np.zeros(N * 2, dtype=np.complex64), CENTER)

    assert np.all(frame.db == iq.DB_FLOOR)
    assert "Infinity" not in json.dumps(frame.as_dict())


def test_a_partial_trailing_segment_is_dropped_not_padded() -> None:
    """Padding would invent a spectrum for time the radio never sampled, and it would
    not fail loudly: half a segment of zeros in a four-segment frame pulls every bin
    down ~1.2 dB, which reads as a slightly weaker signal rather than as a bug."""
    spectro = iq.Spectrometer(N, RATE)
    samples = _tone(N * 3 + N // 2, 60_000)

    partial = spectro.frame(samples, CENTER)
    whole = spectro.frame(samples[: N * 3], CENTER)

    assert partial.segments == 3
    assert np.array_equal(partial.db, whole.db)
    # The level is the tell: a padded frame could not still read full scale.
    assert partial.db.max() == pytest.approx(0.0, abs=0.01)


def test_a_frame_shorter_than_one_segment_is_refused() -> None:
    """There is no honest spectrum to return, and a padded one is the lie above."""
    spectro = iq.Spectrometer(N, RATE)
    with pytest.raises(ValueError, match="whole segment"):
        spectro.frame(_tone(N - 1, 0.0), CENTER)


def test_cs8_samples_convert_to_the_same_picture() -> None:
    """`setupStream` offers CS8 and it halves the USB-side memory traffic, so the
    conversion is here — scaled by 128 rather than 127, which is why a nominally
    full-scale CS8 tone reads a shade under 0 dBFS instead of a shade over."""
    spectro = iq.Spectrometer(N, RATE)
    tone = _tone(N * 4, 60_000, amplitude=0.5)
    interleaved = np.empty(tone.size * 2, dtype=np.float32)
    interleaved[0::2], interleaved[1::2] = np.real(tone), np.imag(tone)
    cs8 = np.round(interleaved * 128.0).astype(np.int8)

    frame = spectro.frame(cs8, CENTER)

    assert frame.bins == N
    assert int(np.argmax(frame.db)) == _bin_of(frame, CENTER + 60_000)
    # 8 bits of quantisation, so the level is close rather than exact.
    assert frame.db.max() == pytest.approx(-6.0206, abs=0.1)

    with pytest.raises(ValueError, match="whole I/Q pairs"):
        iq.as_complex64(np.zeros(N * 2 + 1, dtype=np.int8))


def test_unsupported_sample_formats_are_refused_by_name() -> None:
    """CS16 and u8 are both plausible things to hand this, and both would be silently
    wrong if `astype` were allowed to reinterpret them as real-valued samples."""
    with pytest.raises(TypeError, match="int16"):
        iq.as_complex64(np.zeros(N, dtype=np.int16))
    with pytest.raises(ValueError, match="n must be positive"):
        iq.bin_width_hz(RATE, 0)
    with pytest.raises(ValueError, match="at least 2"):
        iq.Spectrometer(1, RATE)
