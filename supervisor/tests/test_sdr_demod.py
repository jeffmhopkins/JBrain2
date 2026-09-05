"""The sidecar's demodulator — the physics, without a radio.

Loaded by path like every other `deploy/sdr/` module. What is pinned here is the
arithmetic no hardware can help with and none is needed for: a synthetic signal of
known modulation must come out as audio of known frequency and known level, and it
must come out THE SAME whether it arrived in one buffer or forty.

That last one is the test worth having. Every stage of this chain carries state — the
mixer's phase, each filter's tail, the discriminator's previous sample — and every
one of them is invisible when a test feeds a single buffer. Chunk the same signal and
a missing tail becomes a click at every boundary, a missing phase becomes a step in
the carrier, and a missing previous sample becomes an impulse the discriminator turns
into a bang. `test_chunking_changes_nothing` is what makes all three impossible.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

CAPTURE_HZ = 2_400_000


def _load():
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location("sdr_demod", DEPLOY / "sdr/demod.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_demod"] = module
    spec.loader.exec_module(module)
    return module


demod = _load()


# -- signal generators ---------------------------------------------------------------


def fm_signal(
    seconds: float,
    *,
    tone_hz: float,
    deviation_hz: float,
    rate_hz: int = CAPTURE_HZ,
    offset_hz: float = 0.0,
) -> np.ndarray:
    """A carrier at `offset_hz`, frequency-modulated by one tone.

    Built by integrating the instantaneous frequency rather than by the shortcut of
    `cos(wc*t + m*sin(wm*t))`: the integral is what FM *is*, and it stays right when
    the modulation is not a single sinusoid."""
    n = int(seconds * rate_hz)
    t = np.arange(n, dtype=np.float64) / rate_hz
    freq = offset_hz + deviation_hz * np.sin(2.0 * np.pi * tone_hz * t)
    phase = 2.0 * np.pi * np.cumsum(freq) / rate_hz
    return np.exp(1j * phase).astype(np.complex64)


def am_signal(
    seconds: float, *, tone_hz: float, depth: float = 0.8, rate_hz: int = CAPTURE_HZ
) -> np.ndarray:
    n = int(seconds * rate_hz)
    t = np.arange(n, dtype=np.float64) / rate_hz
    return ((1.0 + depth * np.sin(2.0 * np.pi * tone_hz * t)) / 2.0).astype(
        np.complex64
    )


def tone(
    seconds: float, *, hz: float, rate_hz: int = CAPTURE_HZ, amplitude: float = 0.5
) -> np.ndarray:
    n = int(seconds * rate_hz)
    t = np.arange(n, dtype=np.float64) / rate_hz
    return (amplitude * np.exp(2j * np.pi * hz * t)).astype(np.complex64)


def dominant_hz(pcm: np.ndarray, rate_hz: int) -> float:
    """The loudest frequency in a block of audio, ignoring DC.

    Windowed, because a rectangular window on a tone that is not bin-centred leaks
    across the whole spectrum and the argmax lands wherever the leakage is worst."""
    audio = pcm.astype(np.float64)
    audio = audio - audio.mean()
    spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    spec[: max(2, int(50 * audio.size / rate_hz))] = 0.0
    return float(np.argmax(spec)) * rate_hz / audio.size


def rms(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))


def settled(pcm: np.ndarray, rate_hz: int, skip_s: float = 0.05) -> np.ndarray:
    """Audio past the filters' start-up transient.

    The chain's group delay is a few hundred samples and its first outputs are
    convolved against a tail of zeros, so the first few milliseconds are a ramp. Any
    real receiver has the same; measuring across it would just measure the ramp."""
    return pcm[int(skip_s * rate_hz) :]


# -- the shapes of the thing ---------------------------------------------------------


def test_every_mode_builds_at_the_shared_capture_rate():
    """2 400 000 divides every mode's IF — what lets one radio serve them all."""
    for mode in demod.IF_RATE_HZ:
        built = demod.Demodulator(mode, CAPTURE_HZ)
        assert CAPTURE_HZ % built.if_rate_hz == 0
        assert built.if_rate_hz % built.audio_rate_hz == 0


def test_a_capture_rate_that_does_not_divide_is_refused():
    """Refused rather than resampled. Audio that is approximately the right pitch
    drifts against the clock, and nothing downstream can tell."""
    with pytest.raises(demod.DemodError):
        demod.Demodulator("fm", 2_000_001)


def test_an_unknown_mode_is_refused():
    with pytest.raises(demod.DemodError):
        demod.Demodulator("ssb", CAPTURE_HZ)


def test_output_is_the_pcm_rtl_fm_wrote():
    """int16 mono little-endian: the format every consumer downstream already reads."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    out = built.feed(fm_signal(0.2, tone_hz=1_000.0, deviation_hz=3_000.0))
    assert out.pcm.dtype == np.int16
    assert out.tobytes() == out.pcm.tobytes()
    assert len(out.tobytes()) == out.pcm.size * 2


def test_the_output_rate_is_the_audio_rate():
    """0.4 s of capture is 0.4 s of audio, to within the chain's start-up."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    out = built.feed(fm_signal(0.4, tone_hz=1_000.0, deviation_hz=3_000.0))
    assert abs(out.pcm.size - 0.4 * built.audio_rate_hz) < 200


def test_a_tiny_buffer_neither_raises_nor_loses_samples():
    """The radio hands over whatever it has; a caller must not have to round it.

    A twenty-sample buffer is 8 µs and cannot fill the chain, so almost nothing comes
    back — but nothing is DISCARDED either: it sits in the first filter's tail and is
    part of the next buffer's audio. `test_chunking_changes_nothing` is what proves
    that in full; this pins the degenerate end of it, where a naive implementation
    raises on an empty intermediate array."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    assert built.feed(np.zeros(20, dtype=np.complex64)).pcm.size <= 1
    assert built.feed(np.zeros(0, dtype=np.complex64)).pcm.size == 0
    assert (
        built.feed(fm_signal(0.2, tone_hz=1_000.0, deviation_hz=3_000.0)).pcm.size
        > 3_000
    )


# -- the physics ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "deviation_hz", "tone_hz"),
    [("fm", 3_000.0, 1_000.0), ("nfm", 4_000.0, 1_500.0), ("wbfm", 50_000.0, 1_000.0)],
)
def test_fm_recovers_the_modulating_tone(mode, deviation_hz, tone_hz):
    built = demod.Demodulator(mode, CAPTURE_HZ)
    out = built.feed(fm_signal(0.5, tone_hz=tone_hz, deviation_hz=deviation_hz))
    audio = settled(out.pcm, built.audio_rate_hz)
    assert dominant_hz(audio, built.audio_rate_hz) == pytest.approx(tone_hz, abs=40)


def test_fm_level_follows_deviation():
    """Twice the deviation is twice the audio: the discriminator is linear, and an
    AGC anywhere in this chain would destroy the property the level meter reports."""
    quiet = demod.Demodulator("fm", CAPTURE_HZ).feed(
        fm_signal(0.4, tone_hz=1_000.0, deviation_hz=1_500.0)
    )
    loud = demod.Demodulator("fm", CAPTURE_HZ).feed(
        fm_signal(0.4, tone_hz=1_000.0, deviation_hz=3_000.0)
    )
    ratio = loud.peak / quiet.peak
    assert ratio == pytest.approx(2.0, rel=0.12)


def test_full_deviation_reaches_most_of_full_scale():
    """The reason `FM_DEVIATION_HZ` exists: the discriminator's natural output for a
    narrowband signal is a fifth of full scale, which is correct and sounds broken."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    out = built.feed(fm_signal(0.4, tone_hz=1_000.0, deviation_hz=5_000.0))
    assert 0.6 < out.peak <= 1.0


def test_am_recovers_the_modulating_tone():
    built = demod.Demodulator("am", CAPTURE_HZ)
    out = built.feed(am_signal(0.5, tone_hz=1_200.0))
    audio = settled(out.pcm, built.audio_rate_hz)
    assert dominant_hz(audio, built.audio_rate_hz) == pytest.approx(1_200.0, abs=40)


def test_am_removes_the_carrier():
    """An unmodulated carrier is a constant envelope, and constant is DC. If the DC
    blocker were missing this would come out as a rail, not as silence."""
    built = demod.Demodulator("am", CAPTURE_HZ)
    for _ in range(6):  # the blocker's estimate is smoothed; give it a moment
        out = built.feed(np.full(240_000, 0.5, dtype=np.complex64))
    assert out.peak < 0.05


@pytest.mark.parametrize(("mode", "sign"), [("usb", 1.0), ("lsb", -1.0)])
def test_ssb_keeps_its_own_sideband(mode, sign):
    """A tone 1 kHz above the carrier is USB audio at 1 kHz — and is NOT LSB audio at
    all. Rejecting the other sideband is the entire content of single sideband, and a
    real filter applied after the fold cannot do it."""
    built = demod.Demodulator(mode, CAPTURE_HZ)
    wanted = built.feed(tone(0.5, hz=sign * 1_000.0))
    other = demod.Demodulator(mode, CAPTURE_HZ).feed(tone(0.5, hz=-sign * 1_000.0))
    rate = built.audio_rate_hz
    assert dominant_hz(settled(wanted.pcm, rate), rate) == pytest.approx(
        1_000.0, abs=40
    )
    # RMS past the start-up, not the peak over everything: the SSB bandpass is 641
    # taps and its step response against a tail of zeros is a transient far louder
    # than the leakage this is measuring.
    rejection = rms(settled(wanted.pcm, rate)) / max(
        rms(settled(other.pcm, rate)), 1e-9
    )
    assert rejection > 100.0  # 40 dB


def test_offset_tuning_lands_the_signal_at_dc():
    """Why the mixer is here: tuning the LO onto the station puts it on the RTL2832U's
    own DC spike, so the radio is tuned aside and the offset is taken out in software —
    which must produce the same audio as if it had never been offset at all."""
    centred = demod.Demodulator("fm", CAPTURE_HZ).feed(
        fm_signal(0.4, tone_hz=1_000.0, deviation_hz=3_000.0)
    )
    built = demod.Demodulator("fm", CAPTURE_HZ, offset_hz=250_000.0)
    # `offset_hz` is what it SNAPPED to, not what was asked for — the mixer rounds to
    # a whole division of the sample rate so its tone is a short repeating table. A
    # caller that tunes the radio to the requested offset and mixes by the snapped one
    # is left 10 kHz out, which for a narrowband channel is silence; reading the
    # attribute back is the contract, and this test is the one that would catch a
    # caller that did not.
    assert built.offset_hz == pytest.approx(240_000.0)
    aside = built.feed(
        fm_signal(0.4, tone_hz=1_000.0, deviation_hz=3_000.0, offset_hz=built.offset_hz)
    )
    assert aside.peak == pytest.approx(centred.peak, rel=0.05)
    rate = 16_000
    assert dominant_hz(settled(aside.pcm, rate), rate) == pytest.approx(1_000.0, abs=40)


def test_a_signal_outside_the_channel_is_rejected():
    """A station 200 kHz away must not reach the demodulator.

    Measured on the BASEBAND power, not on the audio, and the reason is worth stating
    because it also decides what the level meter can mean: an FM discriminator reports
    the ANGLE between consecutive samples and is completely blind to amplitude. A
    carrier attenuated by 60 dB still has a perfectly good phase ramp, so it still
    demodulates to a full-scale tone. That is why an FM receiver needs a squelch and
    why an empty channel roars — `rtl_fm` ships here without `-l` too, so this is
    parity rather than a regression, but it is the one place where audio level cannot
    answer a question about signal strength."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    inside = built.feed(tone(0.3, hz=2_000.0))
    outside = demod.Demodulator("fm", CAPTURE_HZ).feed(tone(0.3, hz=200_000.0))
    power = lambda a: float(np.mean(np.abs(a.baseband[-4096:]) ** 2))  # noqa: E731
    assert 10.0 * np.log10(power(inside) / power(outside)) > 50.0


# -- the state, which is where the defects live --------------------------------------


@pytest.mark.parametrize("mode", ["fm", "wbfm", "am", "usb"])
def test_chunking_changes_nothing(mode):
    """The same samples in forty buffers must give the same audio as in one.

    This is the test that makes a dropped filter tail, a restarted mixer phase or a
    forgotten discriminator sample impossible — each of them is inaudible on one
    buffer and a click per boundary on many. The buffer sizes are deliberately not a
    multiple of the decimation, which is what exercises the phase carrying."""
    samples = (
        am_signal(0.4, tone_hz=1_200.0)
        if mode == "am"
        else fm_signal(0.4, tone_hz=1_000.0, deviation_hz=3_000.0)
        if mode in ("fm", "wbfm")
        else tone(0.4, hz=1_000.0)
    )
    whole = demod.Demodulator(mode, CAPTURE_HZ).feed(samples).pcm
    chunked = demod.Demodulator(mode, CAPTURE_HZ)
    pieces = [chunked.feed(part).pcm for part in np.array_split(samples, 37)]
    joined = np.concatenate(pieces)
    assert joined.size == whole.size
    # Two int16 counts of slack: the dot products are the same values in the same
    # order, but BLAS is free to batch them differently across the two shapes.
    assert np.max(np.abs(joined.astype(np.int32) - whole.astype(np.int32))) <= 2


def test_a_chunk_boundary_does_not_click():
    """The audible form of the same fault, measured the way an ear would.

    A click is a sample-to-sample jump far larger than the signal's own slew. On a
    1 kHz tone at 16 kHz nothing legitimate moves more than a third of full scale in
    one sample, so anything past that is a discontinuity we introduced."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    samples = fm_signal(0.5, tone_hz=1_000.0, deviation_hz=4_000.0)
    joined = np.concatenate([built.feed(p).pcm for p in np.array_split(samples, 41)])
    audio = settled(joined, built.audio_rate_hz).astype(np.int32)
    assert np.max(np.abs(np.diff(audio))) < 12_000


def test_the_mixer_phase_survives_a_long_run():
    """The reason the phase is an index into one period rather than an accumulated
    float: over an hour at 2.4 MS/s an accumulator reaches ~10^10 radians, where
    float64's spacing exceeds the per-sample step and the tone stops advancing.

    Simulated by winding the counter forward rather than by running for an hour."""
    built = demod.Demodulator("fm", CAPTURE_HZ, offset_hz=250_000.0)
    at = built.offset_hz
    fresh = built.feed(
        fm_signal(0.2, tone_hz=1_000.0, deviation_hz=3_000.0, offset_hz=at)
    )
    aged = demod.Demodulator("fm", CAPTURE_HZ, offset_hz=250_000.0)
    aged._mixer._n = 2_400_000 * 3_600 % max(aged._mixer._period, 1)
    later = aged.feed(
        fm_signal(0.2, tone_hz=1_000.0, deviation_hz=3_000.0, offset_hz=at)
    )
    assert later.peak == pytest.approx(fresh.peak, rel=0.05)


# -- the baseband, which is what the tuning view draws --------------------------------


def test_the_baseband_is_the_channel_at_the_if_rate():
    """`Audio.baseband` is what makes the narrow tuning view free: it is the decimated
    complex stream, so an FFT of it is the channel's own spectrum. 512 bins over
    48 kHz is 94 Hz — six times finer than a 4000-bin transform of the whole 2.4 MHz
    capture, and it costs a 512-point FFT instead of a 4000-point one."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    # A PLAIN carrier, not a modulated one: an FM signal deviating +/-2 kHz has no
    # single frequency to find, and an argmax over it lands anywhere in its swing.
    out = built.feed(tone(0.4, hz=6_000.0))
    assert out.baseband.dtype == np.complex64
    assert out.baseband.size == pytest.approx(0.4 * built.if_rate_hz, abs=100)
    n = 512
    spec = np.abs(np.fft.fftshift(np.fft.fft(out.baseband[-n:] * np.hanning(n))))
    bin_hz = built.if_rate_hz / n
    found = (int(np.argmax(spec)) - n // 2) * bin_hz
    assert found == pytest.approx(6_000.0, abs=1.5 * bin_hz)


def test_the_channel_half_width_is_what_the_filter_actually_keeps():
    """The number the tuning view shades has to be the number the filter uses, or the
    picture says one thing and the radio does another."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    inside = built.feed(tone(0.3, hz=built.channel_half_hz * 0.6))
    outside = demod.Demodulator("fm", CAPTURE_HZ).feed(
        tone(0.3, hz=built.channel_half_hz * 3.0)
    )
    power = lambda a: float(np.mean(np.abs(a.baseband[-4096:]) ** 2))  # noqa: E731
    assert power(inside) > power(outside) * 100
