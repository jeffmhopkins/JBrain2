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
    AGC anywhere in this chain would destroy the property the level meter reports.

    Measured AFTER the chain has settled, and that is not a detail. This read
    `Audio.peak` over the whole buffer, which on a filter chain starting from zeroed
    tails is the start-up transient and nothing else — the first hundred samples were
    the loudest in both buffers, so the test was comparing two step responses and
    calling it linearity. It passed anyway until the channel filter made the step a
    little sharper, at which point the ratio it reported was 1.29."""

    def settled_peak(deviation_hz: float) -> float:
        built = demod.Demodulator("fm", CAPTURE_HZ)
        pcm = built.feed(fm_signal(0.4, tone_hz=1_000.0, deviation_hz=deviation_hz)).pcm
        audio = settled(pcm, built.audio_rate_hz).astype(np.float64)
        return float(np.max(np.abs(audio))) / 32768.0

    assert settled_peak(3_000.0) / settled_peak(1_500.0) == pytest.approx(2.0, rel=0.12)


def test_full_deviation_reaches_most_of_full_scale():
    """The reason `FM_DEVIATION_HZ` exists: the discriminator's natural output for a
    narrowband signal is a fifth of full scale, which is correct and sounds broken.

    Measured on the SETTLED audio, for the reason `test_fm_level_follows_deviation`
    gives: `Audio.peak` spans the whole buffer and the filters start from zeroed tails,
    so the loudest sample in a fresh chain is the step response, not the signal. This
    read `out.peak` and passed only because the audio low-pass was sagging enough to
    hold the transient under 1.0 — fixing that sag (the cutoff was on the passband
    edge) took it to 1.13 and failed the assertion, which is the test noticing a
    quantity it was never measuring."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    pcm = built.feed(fm_signal(0.4, tone_hz=1_000.0, deviation_hz=5_000.0)).pcm
    audio = settled(pcm, built.audio_rate_hz).astype(np.float64)
    assert 0.6 < float(np.max(np.abs(audio))) / 32768.0 <= 1.0


def _tone_level(built, pcm: np.ndarray, hz: float) -> float:
    """The recovered tone's level in dB, past the chain's start-up transient."""
    audio = settled(pcm, built.audio_rate_hz).astype(np.float64)
    mag = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    freqs = np.fft.rfftfreq(audio.size, 1.0 / built.audio_rate_hz)
    at = np.abs(freqs - hz) < 60.0
    return 20.0 * np.log10(max(float(np.sqrt((mag[at] ** 2).sum())), 1e-12))


def _sweep_audio(mode: str, make, hzs: tuple[float, ...]) -> list[float]:
    """The chain's response across FREQUENCY, each point on a fresh chain."""
    out = []
    for hz in hzs:
        built = demod.Demodulator(mode, CAPTURE_HZ)
        out.append(_tone_level(built, built.feed(make(hz)).pcm, hz))
    return out


def test_the_audio_passband_is_flat_where_it_claims_to_be():
    """THE TEST THAT WAS MISSING, and the reason `_build_back` shipped sagging.

    Nothing here measured the chain against FREQUENCY. Every audio test used a single
    tone at 1 kHz, where the defect is 0.9 dB and invisible; tone recovery, level
    linearity and chunk invariance all pass on a filter whose passband is a slope.

    AM is asserted on because it has no de-emphasis: its passband should simply be
    flat. Measured with the cutoff on the passband edge it was ~2 dB down at 3 kHz with
    nothing to blame it on."""
    hzs = (300.0, 1_000.0, 2_000.0, 3_000.0)
    levels = _sweep_audio("am", lambda hz: am_signal(0.5, tone_hz=hz), hzs)
    for hz, level in zip(hzs[1:], levels[1:], strict=True):
        assert level - levels[0] > -1.5, (
            f"AM is {levels[0] - level:.1f} dB down at {hz} Hz"
        )


def test_fm_audio_is_shaped_by_de_emphasis_AND_NOTHING_ELSE():
    """The FM half of the same question, and the sharper form of it.

    FM's passband is deliberately tilted, so "flat" is the wrong assertion — but the
    tilt has an exact shape, `1/sqrt(1 + (2*pi*f*tau)^2)`, and anything the anti-alias
    filter adds on top of it is a defect. With the cutoff on the passband edge the
    chain was 1.8 dB below the ideal curve at 3 kHz; it now tracks it to a tenth."""
    tau = demod.DEEMPHASIS_S
    hzs = (300.0, 1_000.0, 2_000.0, 3_000.0)
    levels = _sweep_audio(
        "fm", lambda hz: fm_signal(0.5, tone_hz=hz, deviation_hz=2_000.0), hzs
    )
    for hz, level in zip(hzs, levels, strict=True):
        ideal = -10.0 * np.log10(1.0 + (2.0 * np.pi * hz * tau) ** 2)
        got = level - levels[0]
        want = ideal - (-10.0 * np.log10(1.0 + (2.0 * np.pi * hzs[0] * tau) ** 2))
        assert abs(got - want) < 0.5, (
            f"{hz} Hz is {got:.2f} dB, de-emphasis wants {want:.2f}"
        )


def test_the_receivers_own_dc_spike_lands_outside_every_channel():
    """`LISTEN_OFFSET_HZ` exists to move the station off the receiver's DC spike, and
    it only does that if the DECIMATION does not fold the spike back on top of it.

    240 kHz — the value shipped until 2026-09-06 — is exactly 5x the 48 kHz IF and 1x
    the 240 kHz one, so the spike aliased to 0 Hz: the tuned frequency, dead centre of
    the channel, at -65 dB. An empty channel then carries a residual carrier the tuning
    strip cannot tell from a station, which is the same false positive the front-end sag
    produced. The offset is a MEASUREMENT, not a round number."""
    # By path, like `demod` itself: `deploy/sdr/` is not on the type-checker's
    # path, and a bare import resolves only because `_load` put it on sys.path.
    listen = importlib.import_module("listen")
    for mode in ("nfm", "fm", "wbfm", "am"):
        built = demod.Demodulator(
            mode, listen.LISTEN_CAPTURE_HZ, offset_hz=float(listen.LISTEN_OFFSET_HZ)
        )
        bias = np.ones(listen.LISTEN_CAPTURE_HZ // 10, dtype=np.complex64)
        channel = None
        for _ in range(5):  # let every tail fill with the spike
            stream = built._mixer.feed(bias)
            for stage in built._front:
                stream = stage.feed(stream)
            channel = built._channel.feed(stream) if built._channel else stream
        assert channel is not None
        bins = 1024
        spec = np.zeros(bins)
        for start in range(0, channel.size - bins + 1, bins):
            window = channel[start : start + bins] * np.hanning(bins)
            spec += np.abs(np.fft.fftshift(np.fft.fft(window))) ** 2
        where = (float(np.argmax(spec)) - bins / 2) * (built.if_rate_hz / bins)
        assert abs(where) > built.channel_half_hz, (
            f"{mode}: the DC spike folds to {where:.0f} Hz, inside a "
            f"+/-{built.channel_half_hz:.0f} Hz channel"
        )


def test_wide_fm_builds_no_channel_filter():
    """Its own docstring says so, and for a while it did not: the guard let a 90 kHz
    channel in a 240 kHz IF through, building 53 taps for 18% of the chain's cost and
    narrowing the signal. The front end already band-limits wide FM to its channel."""
    assert demod.Demodulator("wbfm", CAPTURE_HZ)._channel is None
    assert demod.Demodulator("nfm", CAPTURE_HZ)._channel is not None


def test_am_recovers_the_modulating_tone():
    built = demod.Demodulator("am", CAPTURE_HZ)
    out = built.feed(am_signal(0.5, tone_hz=1_200.0))
    audio = settled(out.pcm, built.audio_rate_hz)
    assert dominant_hz(audio, built.audio_rate_hz) == pytest.approx(1_200.0, abs=40)


def test_am_removes_the_carrier():
    """An unmodulated carrier is a constant envelope, and constant is DC. If the DC
    blocker were missing this would come out as a rail, not as silence."""
    built = demod.Demodulator("am", CAPTURE_HZ)
    carrier = np.full(240_000, 0.5, dtype=np.complex64)
    # Several buffers: the blocker's window is 512 audio samples, so the first one is
    # still filling it and its output is the ramp rather than the answer.
    out = built.feed(carrier)
    for _ in range(5):
        out = built.feed(carrier)
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


def test_the_baseband_is_the_if_the_picture_is_drawn_from():
    """`Audio.baseband` is what makes the narrow tuning view free: it is the decimated
    complex stream, so an FFT of it is the tuned neighbourhood's spectrum. 512 bins over
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


def test_the_picture_is_flat_across_the_view():
    """PURE NOISE MUST LOOK LIKE PURE NOISE, and this is the regression test for the
    evening it did not.

    The front end used to be the channel filter as well as the anti-alias one, with
    its cutoff asked for at the passband edge — and a windowed sinc's cutoff is where
    it is SIX DECIBELS DOWN, so the response sagged from 0 dB at DC to -4.7 dB at the
    edge of the channel and -21 dB just outside it. `Audio.baseband` is what the tuning
    strip draws, so every listening session drew that sag.

    On air (2026-09-06) that meant an EMPTY channel — no station, receiver noise only —
    arrived as a smooth hump centred exactly on the tuned frequency, reading 13.7 dB
    over its own outer bins. The strip drew a strong station, the readout said "On
    centre", `listen-probe` returned `ok: True`, and the owner heard the static that
    noise through a discriminator actually is. Every instrument agreed, and all of them
    were reporting the shape of our own filter.

    So: flat, to a decibel, across the whole width the picture claims to show."""
    built = demod.Demodulator("nfm", CAPTURE_HZ)
    rng = np.random.default_rng(11)
    bins = 512
    spec = np.zeros(bins)
    for _ in range(8):
        n = CAPTURE_HZ // 10
        noise = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02).astype(
            np.complex64
        )
        row = built.feed(noise).baseband
        # Averaged over many windows: one periodogram of noise IS noise, and a
        # flatness test run on one would be measuring its own estimator.
        for start in range(0, row.size - bins, bins):
            window = row[start : start + bins] * np.hanning(bins)
            spec += np.abs(np.fft.fftshift(np.fft.fft(window))) ** 2
    db = 10.0 * np.log10(spec / spec.max())
    away = np.abs((np.arange(bins) - bins / 2) * (built.if_rate_hz / bins))
    centre = db[away <= 2_000.0].mean()
    # The ring the probe and the strip both take their noise floor from: outside the
    # passband, inside the crop. On the old chain this sat 11 dB below the centre and
    # every reading of "signal over floor" was really a reading of that.
    half = built.channel_half_hz
    ring = db[(away > half) & (away <= 2.0 * half)].mean()
    assert abs(centre - ring) < 1.5
    # ...and the same numbers the probe prints: max over median, across the crop.
    shown = db[away <= 2.0 * built.channel_half_hz]
    assert float(shown.max() - np.median(shown)) < 8.0


def _row(built, offset_hz: float, bins: int = 512) -> tuple[np.ndarray, float]:
    """The picture a plain carrier at `offset_hz` makes, in dB, and its bin spacing."""
    out = None
    for _ in range(5):  # let every tail fill
        n = CAPTURE_HZ // 10
        tone = np.exp(2j * np.pi * offset_hz * np.arange(n) / CAPTURE_HZ)
        out = built.feed(tone.astype(np.complex64)).baseband
    assert out is not None and out.size >= bins
    spec = np.zeros(bins)
    for start in range(0, out.size - bins + 1, bins):
        window = out[start : start + bins] * np.hanning(bins)
        spec += np.abs(np.fft.fftshift(np.fft.fft(window))) ** 2
    return 10.0 * np.log10(np.maximum(spec, 1e-30)), built.view_rate_hz / bins


def test_the_picture_is_wider_than_the_crop_in_every_mode():
    """The strip crops to TWICE the passband, so a picture narrower than that is the
    front end's own roll-off being drawn as if it were spectrum."""
    for mode in demod.IF_RATE_HZ:
        built = demod.Demodulator(mode, CAPTURE_HZ)
        assert built.view_half_hz >= 2.0 * built.channel_half_hz, mode


@pytest.mark.parametrize("mode", ["nfm", "wbfm"])
def test_nothing_outside_the_picture_folds_into_it(mode):
    """A signal the picture does not cover must not APPEAR in it somewhere else.

    The regression test for breaking wide FM while fixing narrow FM. Flattening the
    front end (`test_the_picture_is_flat_across_the_view`) also stopped it rejecting
    what folds down: with the picture taken at the 240 kHz IF, the anti-alias filter
    only had to reach its stopband by 144 kHz, so everything from 120 to 144 kHz
    landed back inside the row — and a carrier 200 kHz out, which on the FM broadcast
    raster is EXACTLY where the next station sits, arrived at -40 kHz at full
    strength. Measured on air 2026-09-06: 104.1 reported its strongest bin 100 kHz off
    centre, and 96.5 — a station `rtl_power` sees 13 dB up — read 2.8 dB over a floor
    its own neighbours were holding up.

    The fix is that the picture is taken at `view_rate_hz`, above the decimation that
    was folding. What is asserted is the property, not the mechanism: nothing from
    outside the view may put energy into the part of the row the strip draws."""
    built = demod.Demodulator(mode, CAPTURE_HZ)
    centred, bin_hz = _row(built, 0.0)
    reference = float(centred.max())
    bins = centred.size
    away = np.abs((np.arange(bins) - bins / 2) * bin_hz)
    shown = away <= 2.0 * built.channel_half_hz  # what `listen._tuning_frame` keeps
    for step in (1.3, 1.6, 2.0, 2.5, 3.0):
        offset = built.view_half_hz * step
        db, _ = _row(built, offset)
        worst = float(db[shown].max())
        assert worst < reference - 40.0, (
            f"{offset / 1000:.0f} kHz reached {worst - reference:.1f} dB"
        )


def test_the_channel_filter_keeps_the_channel_and_drops_the_rest():
    """The number the tuning view SHADES has to be the number the audio filter uses.

    Measured on the audio now rather than on `baseband`, because those are deliberately
    two different widths since the split: `baseband` is the picture, `view_half_hz`
    wide, and the channel filter runs after it. A test on `baseband` would now be
    testing the anti-alias filter and would pass whatever the channel filter did."""
    built = demod.Demodulator("fm", CAPTURE_HZ)
    half = built.channel_half_hz
    inside = built.feed(
        fm_signal(0.4, tone_hz=1_000.0, deviation_hz=2_000.0, offset_hz=0.0)
    )
    aside = demod.Demodulator("fm", CAPTURE_HZ).feed(
        fm_signal(0.4, tone_hz=1_000.0, deviation_hz=2_000.0, offset_hz=half * 2.5)
    )
    rate = built.audio_rate_hz

    # The TONE, not the level: an FM discriminator fed almost nothing still emits
    # full-scale noise (see `test_a_signal_outside_the_channel_is_rejected`), so the
    # question a rejected station can answer is whether its modulation survives.
    def tone_share(pcm: np.ndarray) -> float:
        audio = settled(pcm, rate).astype(np.float64)
        mag = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
        freqs = np.fft.rfftfreq(audio.size, 1.0 / rate)
        at = np.abs(freqs - 1_000.0) < 60.0
        return float((mag[at] ** 2).sum() / max((mag**2).sum(), 1e-30))

    assert tone_share(inside.pcm) > 0.5
    assert tone_share(aside.pcm) < 0.01
