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
        # 0.5 dB, not the 1.5 this first used: the fixed chain measures 0.08 dB of
        # droop and the broken one 1.93, so 1.5 left only 0.43 dB of margin — and the
        # test would have passed the OLD code had the sweep stopped at 2 kHz.
        assert level - levels[0] > -0.5, (
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


def _dc_leak_db(mode: str, capture_hz: int, offset_hz: float) -> tuple[float, float]:
    """How much of a unit DC input survives to the discriminator, and to the picture.

    RMS, in dB, for a DC bias of amplitude 1 — which is what the receiver's own LO
    leakage looks like before the mixer moves it. Two numbers because there are two
    places it can hurt: the audio, and the row the tuning strip draws."""
    built = demod.Demodulator(mode, capture_hz, offset_hz=offset_hz)
    return _dc_leak_db_of(built, capture_hz)


def _dc_leak_db_of(built, capture_hz: int) -> tuple[float, float]:
    """The same measurement on a chain already built, so a test can strip a stage."""
    bias = np.ones(capture_hz // 10, dtype=np.complex64)
    channel = view = None
    for _ in range(6):  # let every filter tail fill with the spike
        stream = built._mixer.feed(bias)
        wide = built._view_row(stream) if built._view is not None else None
        for stage in built._front:
            stream = stage.feed(stream)
        view = wide if wide is not None else stream
        channel = built._channel.feed(stream) if built._channel else stream

    def rms(x: np.ndarray) -> float:
        return 20.0 * np.log10(max(float(np.sqrt(np.mean(np.abs(x) ** 2))), 1e-30))

    assert channel is not None and view is not None
    return rms(channel), rms(view)


def test_the_receivers_own_dc_spike_is_suppressed_in_every_mode():
    """`LISTEN_OFFSET_HZ` exists to keep the receiver's DC spike off the station, and it
    only does that if the DECIMATION does not fold it back.

    240 kHz — the value shipped until 2026-09-06 — is exactly 5x the 48 kHz IF and 1x
    the 240 kHz one, so the spike aliased to 0 Hz: the tuned frequency, dead centre.

    **Asserted as a LEVEL, and that is the whole point of this test's second draft.**
    The first asserted POSITION — that the fold lands outside `channel_half_hz` — and
    that is not the same question: it passed wide FM at -11 dBFS, certifying as fixed a
    52 dB regression, and it had to exclude usb/lsb from its mode list because a
    residue of -158 dB puts the argmax on an arbitrary bin. A test that must drop modes
    to pass is measuring the wrong thing.

    The view is asserted too: it is what the strip draws, and for a wide-FM picture
    taken at `view_rate_hz` the spike can sit in the row unattenuated while the audio is
    perfectly clean."""
    listen = importlib.import_module("listen")
    for mode in demod.IF_RATE_HZ:
        audio, view = _dc_leak_db(
            mode, listen.LISTEN_CAPTURE_HZ, float(listen.LISTEN_OFFSET_HZ)
        )
        assert audio < -60.0, f"{mode}: DC leaks into the audio at {audio:.1f} dBFS"
        assert view < -40.0, f"{mode}: DC sits in the picture at {view:.1f} dBFS"


def test_the_shipped_offset_beats_the_one_it_replaced():
    """The claim the constant is chosen on, kept honest.

    Every divisor of the capture rate is a candidate and most are worse; this pins that
    the shipped value really is better than its predecessor on BOTH quantities, so a
    future change to `IF_RATE_HZ` that quietly breaks the relationship fails here."""
    listen = importlib.import_module("listen")
    capture = listen.LISTEN_CAPTURE_HZ
    for mode in demod.IF_RATE_HZ:
        now = _dc_leak_db(mode, capture, float(listen.LISTEN_OFFSET_HZ))
        was = _dc_leak_db(mode, capture, 240_000.0)
        assert now[0] <= was[0] + 1.0, f"{mode}: audio {was[0]:.1f} -> {now[0]:.1f}"
        assert now[1] <= was[1] + 1.0, f"{mode}: view {was[1]:.1f} -> {now[1]:.1f}"


def test_wide_fm_has_a_channel_filter_and_needs_it():
    """C5 proposed deleting this filter and was WITHDRAWN after an adversarial review.

    `_build_channel`'s docstring said wide FM gets none because 90 kHz of channel in a
    240 kHz IF "is already most of Nyquist, so the front end is its own channel filter".
    The front end's stages are placed by the MIDPOINT rule, which puts their 6 dB points
    at 240 and 120 kHz — and 120 kHz is not 90. Without this filter the demodulator
    hears 30 kHz beyond its channel on each side, which is the guard band where the
    neighbouring station's skirts live.

    Asserted as SELECTIVITY, measured on the filter, because that is the filter's job.
    (A first draft of this test tried to prove it through the LO spike and found the
    filter buying 0 dB — true, and the wrong instrument: at the shipped offset the
    front end suppresses the spike before the fold, so it never reaches this stage.)"""
    built = demod.Demodulator("wbfm", CAPTURE_HZ)
    channel = built._channel
    assert channel is not None, "wide FM must have a channel filter"
    assert demod.Demodulator("nfm", CAPTURE_HZ)._channel is not None

    taps = channel._taps[::-1]

    def at(hz: float) -> float:
        turns = np.arange(taps.size) * hz / built.if_rate_hz
        gain = abs((taps * np.exp(-2j * np.pi * turns)).sum())
        return 20.0 * np.log10(max(gain, 1e-12))

    assert at(built.channel_half_hz) > -1.0, "it must pass its own channel"
    assert at(115_000.0) < -30.0, "it must reject what the front end still passes"


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


# -- W6a: the filter design is a specification ---------------------------------------


def _response(taps: np.ndarray, hz: np.ndarray, rate_hz: float) -> np.ndarray:
    """|H(f)| of a linear-phase FIR at these frequencies."""
    k = np.arange(taps.size) - (taps.size - 1) / 2.0
    return np.abs(np.exp(-2j * np.pi * np.outer(hz, k) / rate_hz) @ taps)


def test_lowpass_delivers_the_stopband_it_was_ASKED_for():
    """C12's whole point. Under Hamming the stopband was a property of the WINDOW —
    ~-53 dB whatever anyone wanted — so a chain could not promise a number. Kaiser takes
    it as an input, and these three prove it is honoured rather than approached."""
    rate = 48_000.0
    for atten_db in (40.0, 60.0, 90.0):
        h = demod.lowpass(4_000.0, 6_000.0, atten_db, rate)
        hz = np.arange(6_000.0, rate / 2, 25.0)
        worst = 20 * np.log10(_response(h, hz, rate).max())

        assert worst <= -atten_db + 1.0, (
            f"{atten_db} dB asked, {worst:.1f} dB delivered"
        )
        # ...and not wildly OVER-delivered either: paying for 90 dB and getting 130
        # means the taps were bought for nothing.
        assert worst >= -atten_db - 12.0


def test_a_stronger_stopband_costs_taps_and_a_wider_transition_saves_them():
    """The two knobs, each moving the length the direction the design formula says.
    A test that only checked the stopband would pass on a filter of 4000 taps."""
    narrow = demod.lowpass(4_000.0, 5_000.0, 80.0, 48_000.0)
    wide = demod.lowpass(4_000.0, 8_000.0, 80.0, 48_000.0)
    weak = demod.lowpass(4_000.0, 5_000.0, 40.0, 48_000.0)

    assert wide.size < narrow.size
    assert weak.size < narrow.size
    # Odd, always: an even-length linear-phase filter delays by half a sample.
    assert all(h.size % 2 == 1 for h in (narrow, wide, weak))


def test_a_low_pass_is_stated_as_two_EDGES_so_the_6_dB_question_cannot_be_asked():
    """The signature is the fix, not the window. `cutoff_hz` was the 6 dB point and read
    as the passband edge TWICE in this file — `_build_front` and then `_build_back`,
    months apart. Passing both edges leaves nothing to misread: the passband is flat
    where it says it is, and the 6 dB point lands in the middle by construction."""
    h = demod.lowpass(4_000.0, 6_000.0, 80.0, 48_000.0)

    flat = _response(h, np.arange(0.0, 4_000.0, 50.0), 48_000.0)
    assert 20 * np.log10(flat.min()) > -0.2, "the passband must be flat where it claims"
    six = _response(h, np.array([5_000.0]), 48_000.0)[0]
    assert -6.5 < 20 * np.log10(six) < -5.5, "the 6 dB point belongs at the midpoint"


def test_a_stopband_above_nyquist_is_refused_rather_than_clamped():
    """A request the sample rate cannot carry. Moving it quietly would hand back a
    filter that does not do what was asked while looking like one that does."""
    with pytest.raises(demod.DemodError, match="Nyquist"):
        demod.lowpass(4_000.0, 30_000.0, 80.0, 48_000.0)
    with pytest.raises(demod.DemodError, match="above passband"):
        demod.lowpass(6_000.0, 4_000.0, 80.0, 48_000.0)


def _folds_into_the_channel(mode: str) -> float:
    """The worst thing anywhere in the capture band that reaches the CHANNEL, in dB
    relative to a signal dead centre. This is the number C12 is about: everything above
    the anti-alias stopband lands inside the channel on the way down, and a
    discriminator is blind to amplitude, so it becomes full-scale hiss."""
    built = demod.Demodulator(mode, CAPTURE_HZ)
    hz = np.arange(-CAPTURE_HZ / 2, CAPTURE_HZ / 2, 25.0)
    gain = np.ones(hz.size)
    landed = hz.copy()
    rate = float(CAPTURE_HZ)
    for stage in built._front:
        gain = gain * _response(stage._taps[::-1], landed, rate)
        rate = rate / stage._m
        landed = (landed + rate / 2) % rate - rate / 2  # where sampling puts it
    if built._channel is not None:
        gain = gain * _response(built._channel._taps[::-1], landed, rate)
    half = built.channel_half_hz
    inside = np.abs(hz) <= half
    folded = ~inside & (np.abs(landed) <= half)
    return float(20 * np.log10(gain[folded].max() / gain[inside].max()))


@pytest.mark.parametrize("mode", ["wbfm", "nfm", "am", "usb"])
def test_nothing_from_outside_the_channel_arrives_louder_than_the_spec(mode):
    """MEASURED. Hamming gave -55.0 dB (wbfm) to -55.9 (the narrow modes), which is
    C12's reported -56 dB reproduced from the taps alone. At `STOPBAND_DB = 80` the same
    sweep gives -79.1 to -81.3: a local blowtorch 60-70 dB over a weak station stops
    being audible in it.

    The margin is 3 dB rather than 0 because the cascade's own passband ripple and the
    formula's fit both move the last decibel."""
    assert _folds_into_the_channel(mode) <= -demod.STOPBAND_DB + 3.0


@pytest.mark.parametrize(
    "mode,rate", [("wbfm", 240_000), ("nfm", 48_000), ("am", 48_000), ("usb", 48_000)]
)
def test_a_capture_rate_that_needs_no_decimation_builds_and_runs(mode, rate):
    """C24. `_build_front` asked for a filter even when nothing folds, whose stopband
    landed exactly on Nyquist — so every one of these raised `DemodError` from the
    CONSTRUCTOR. Unreachable from `listen.py` at today's rates and a trap for the next
    one added, which is the kind of thing that gets found at 2 a.m. on hardware."""
    built = demod.Demodulator(mode, rate)
    assert built._front == []

    audio = built.feed(tone(0.2, hz=1_000.0, rate_hz=rate))

    assert audio.pcm.size > 0


def test_the_dc_block_has_the_response_its_docstring_CLAIMS():
    """C15. It claimed a corner at `rate / n` — 31 Hz — and 31 Hz is the boxcar's first
    NULL, where a subtracted average leaves the signal alone. The real corner is a
    quarter of that, and there is a +2 dB bump the claim did not mention at all.

    Pinned rather than fixed: the bump is inherent to `x - boxcar(x)` and sits at 20 Hz,
    below anything an AM voice channel carries. What a test can stop is the number
    drifting back to the one that reads plausible."""
    n, rate = demod.DC_BLOCK_TAPS, float(demod.AUDIO_RATE)
    hz = np.arange(0.1, 500.0, 0.1)
    box = (np.exp(-2j * np.pi * np.outer(hz, np.arange(n)) / rate) @ np.ones(n)) / n
    db = 20 * np.log10(np.abs(1.0 - box))

    corner = hz[np.argmin(np.abs(db + 3.0))]
    assert 7.0 < corner < 8.0, f"-3 dB at {corner:.2f} Hz"
    assert abs(corner - 0.24 * rate / n) < 0.5, "≈ 0.24 * rate/n, not rate/n"
    # The first null, where the claim used to point: essentially untouched.
    assert abs(db[np.argmin(np.abs(hz - rate / n))]) < 0.3
    # ...and the overshoot the docstring now names.
    assert 1.8 < db.max() < 2.2 and 18.0 < hz[np.argmax(db)] < 23.0

    # It still does the job it exists for: a DC bias comes out as nothing.
    block = demod._DcBlock(n)
    out = block.feed(np.ones(4 * n, dtype=np.float32))
    assert abs(float(out[-1])) < 1e-3


# -- W6b: the AGC (C13) --------------------------------------------------------------


def _steady_rms_dbfs(pcm: np.ndarray) -> float:
    """Audio level once the AGC's own quarter-second window has filled."""
    x = pcm.astype(np.float64)[pcm.size // 3 :] / 32768.0
    return 20.0 * np.log10(max(float(np.sqrt(np.mean(x**2))), 1e-9))


def _at_rf(mode: str, amplitude: float, seconds: float = 1.5):
    if mode in ("fm", "nfm", "wbfm"):
        signal = fm_signal(seconds, tone_hz=1_000.0, deviation_hz=3_000.0) * amplitude
    elif mode == "am":
        signal = am_signal(seconds, tone_hz=1_000.0) * amplitude
    else:
        # The sign is the sideband. A +1500 Hz tone is an UPPER sideband one and `lsb`
        # is right to reject it — which an earlier cut of this test read as "the AGC
        # failed" rather than as the sideband filter working.
        signal = tone(
            seconds, hz=1_500.0 if mode == "usb" else -1_500.0, amplitude=amplitude
        )
    return demod.Demodulator(mode, CAPTURE_HZ).feed(signal.astype(np.complex64))


@pytest.mark.parametrize("mode", ["am", "usb", "lsb"])
def test_the_same_station_arrives_at_the_same_LOUDNESS_however_strong_it_is(mode):
    """C13. AM and SSB carry the RF level straight through to the audio, so without an
    AGC the same station is as loud as the propagation happens to make it.

    MEASURED before this, at one RF level: nfm -11.4 dBFS, usb -23.0, am -31.0 — a 20 dB
    swing on a MODE CHANGE, and a weak AM or SSB station simply inaudible."""
    levels = [_steady_rms_dbfs(_at_rf(mode, amp).pcm) for amp in (0.5, 0.05)]

    # A hundredfold in RF, within a decibel in what comes out of the speaker.
    assert abs(levels[0] - levels[1]) < 1.0, levels
    assert all(abs(db - 20 * np.log10(demod.AGC_TARGET_RMS)) < 1.0 for db in levels), (
        levels
    )


def test_fm_keeps_its_level_because_its_level_MEANS_something():
    """The other half of C13, and the reason this is not applied everywhere. An FM
    discriminator's output is the DEVIATION, which `FM_DEVIATION_HZ` scales to full
    scale — so FM already arrives at a level that says something about the transmitter,
    and an AGC would replace it with a level that says nothing."""
    strong = _at_rf("nfm", 0.5)
    weak = _at_rf("nfm", 0.05)

    assert strong.gain_db == 0.0 and weak.gain_db == 0.0
    # ...and FM's level does not follow the RF level either, which is the property that
    # makes an AGC pointless here rather than merely unnecessary.
    assert abs(_steady_rms_dbfs(strong.pcm) - _steady_rms_dbfs(weak.pcm)) < 0.5


def test_the_level_METER_is_measured_before_the_gain():
    """`peak` and `rms` are the only honest answer this chain gives to "how strong is
    the signal", and `listen-probe` reads them to decide whether anything is on the air.
    An AGC that moved them would make a dead channel and a loud one report the same
    number — which is precisely what `_to_pcm`'s old comment said it refused to do."""
    strong = _at_rf("usb", 0.5)
    weak = _at_rf("usb", 0.005)

    assert strong.rms > 10.0 * weak.rms, (strong.rms, weak.rms)
    # ...while what the owner HEARS is within a decibel, and the field says by how much.
    assert abs(_steady_rms_dbfs(strong.pcm) - _steady_rms_dbfs(weak.pcm)) < 1.0
    assert weak.gain_db - strong.gain_db > 30.0


def test_the_gain_is_bounded_so_an_empty_channel_stays_quiet():
    """Unbounded, this would amplify a dead channel's noise to full scale — which sounds
    like a fault and hides the fact that nothing is there."""
    silence = demod.Demodulator("am", CAPTURE_HZ).feed(
        np.zeros(CAPTURE_HZ, dtype=np.complex64)
    )

    assert silence.gain_db <= demod.AGC_MAX_GAIN_DB + 1e-6
    assert _steady_rms_dbfs(silence.pcm) < -60.0


def test_the_agc_gain_is_per_SAMPLE_not_per_buffer():
    """A gain that steps once per buffer is a click at every boundary whenever the level
    is moving, and makes the audio a function of how the samples were delivered. This is
    the same defect `_DcBlock`'s docstring records, one stage further down —
    `test_chunking_changes_nothing` covers am and usb and would catch it, and this says
    out loud which property makes that pass."""
    agc = demod._Agc(demod.AUDIO_RATE)
    quiet = np.full(demod.AUDIO_RATE // 4, 0.01, dtype=np.float32)
    loud = np.full(demod.AUDIO_RATE // 4, 0.5, dtype=np.float32)

    agc.feed(quiet)
    out, _ = agc.feed(loud)

    # The gain rides down ACROSS the buffer rather than stepping at its head.
    assert out[0] > 3.0 * out[-1]
    assert np.all(np.diff(out) <= 1e-6), "monotonic, not a step"


# -- filter bandwidth ----------------------------------------------------------------


def _two_am(
    seconds: float, *, wanted_hz: float, neighbour_hz: float, spacing_hz: float
) -> np.ndarray:
    """Two equally strong AM stations, `spacing_hz` apart, each modulated by one tone.

    The measurement this exists for is a RATIO between the two tones in one block of
    audio, which is why both stations are the same strength and why the AGC — on for
    AM — cannot influence the answer: it moves both by the same amount."""
    n = int(seconds * CAPTURE_HZ)
    t = np.arange(n, dtype=np.float64) / CAPTURE_HZ
    wanted = 1.0 + 0.5 * np.cos(2.0 * np.pi * wanted_hz * t)
    neighbour = (1.0 + 0.5 * np.cos(2.0 * np.pi * neighbour_hz * t)) * np.exp(
        2j * np.pi * spacing_hz * t
    )
    return (wanted + neighbour).astype(np.complex64)


def _fed(built, samples: np.ndarray) -> np.ndarray:
    """Every sample through the chain in realistic chunks, as one block of audio."""
    step = CAPTURE_HZ // 10
    out = [built.feed(samples[i : i + step]).pcm for i in range(0, samples.size, step)]
    return np.concatenate(out)


@pytest.mark.parametrize(
    "bandwidth_hz,floor_db",
    # MEASURED 2026-09-08, then floored well under the measurement so the test pins the
    # behaviour rather than the noise: 8 kHz measured 14.8 dB, 6 kHz 85.2, 4 kHz 110.0,
    # 3 kHz 103.1. The gap between the first two rungs is the whole feature — 8 kHz is
    # the widest filter that costs no audio, and 6 kHz is the first that actually
    # rejects a shortwave neighbour 5 kHz away.
    [(8_000, 10.0), (6_000, 60.0), (4_000, 80.0), (3_000, 80.0)],
)
def test_am_bandwidth_rejects_the_neighbour(bandwidth_hz, floor_db):
    """A station 5 kHz away must fall as the filter narrows — the owner's own case.

    5 kHz is the shortwave broadcast raster, so this is not a contrived spacing: it is
    what a crowded 49 m evening actually looks like.

    The rejection has to happen HERE, in the channel filter, because the envelope
    detector downstream is non-linear: two carriers that both reach it beat together and
    their products land inside the audio band, where no filter can tell them from the
    wanted audio afterwards."""
    demod = _load()
    built = demod.Demodulator("am", CAPTURE_HZ, bandwidth_hz=bandwidth_hz)
    pcm = _fed(
        built, _two_am(0.5, wanted_hz=1_000.0, neighbour_hz=1_700.0, spacing_hz=5_000.0)
    )
    wanted = _tone_level(built, pcm, 1_000.0)
    neighbour = _tone_level(built, pcm, 1_700.0)
    assert wanted - neighbour >= floor_db, (
        f"{bandwidth_hz} Hz left the neighbour only {wanted - neighbour:.1f} dB down"
    )


def test_am_default_is_the_widest_that_costs_no_audio():
    """8 kHz must pass AM's own audio band flat — the reason it is the default.

    The old default was 16 kHz, and dropping it was free precisely because of this: the
    audio path is low-passed at 4 kHz, so RF past ±4 kHz carries no wanted audio and the
    extra width only fed the detector interference."""
    demod = _load()
    built = demod.Demodulator("am", CAPTURE_HZ)
    assert built.bandwidth_hz == 8_000
    levels = [
        _tone_level(built, _fed(built, am_signal(0.4, tone_hz=hz)), hz)
        for hz in (300.0, 1_000.0, 2_000.0, 3_000.0)
    ]
    # Flat across the band the audio filter keeps. A channel filter that had started
    # eating its own passband would show up here as a slope.
    assert max(levels) - min(levels) < 3.0, levels


def test_ssb_bandwidth_moves_the_outer_edge_only():
    """Narrowing SSB must not move the edge nearest the suppressed carrier.

    The low edge is set by where the carrier sits, not by how much bandwidth is wanted:
    moving it would walk the passband onto the carrier as the filter narrowed."""
    demod = _load()
    for bandwidth_hz in demod.BANDWIDTH_HZ["usb"]:
        low, high = demod.passband_for("usb", bandwidth_hz)
        assert low == demod.SSB_LOW_HZ
        assert high - low == bandwidth_hz
        # ...and lsb is its mirror, not a copy with a sign bolted on.
        mirror_low, mirror_high = demod.passband_for("lsb", bandwidth_hz)
        assert (mirror_low, mirror_high) == (-high, -low)


def test_a_narrow_ssb_filter_rejects_a_tone_past_its_edge():
    """The generalised bandpass must actually be the width it claims.

    A tone 2.6 kHz off the carrier is inside 3.1 kHz USB and outside 1.8 kHz, so the two
    filters must disagree about it — which is what proves the width reached the taps and
    not merely the label."""
    demod = _load()
    levels = {}
    for bandwidth_hz in (3_100, 1_800):
        built = demod.Demodulator("usb", CAPTURE_HZ, bandwidth_hz=bandwidth_hz)
        pcm = _fed(built, tone(0.4, hz=2_600.0))
        levels[bandwidth_hz] = _tone_level(built, pcm, 2_600.0)
    assert levels[3_100] - levels[1_800] > 25.0, levels


def test_a_width_outside_the_range_is_refused():
    """Bounded, never clamped. A clamped width would leave the radio listening at
    something other than the number on screen, which is the failure this control exists
    to end — and it is one nobody can see or hear.

    Between the bounds anything on the grid is allowed: the owner drags the passband
    edge on the picture and it has to land where they put it, which is a position rather
    than a menu choice."""
    demod = _load()
    low, high = demod.BANDWIDTH_RANGE_HZ["am"]
    # A width no preset names is fine, because the drag can ask for it.
    assert demod.Demodulator("am", CAPTURE_HZ, bandwidth_hz=5_000).bandwidth_hz == 5_000
    for outside in (low - demod.BANDWIDTH_STEP_HZ, high + demod.BANDWIDTH_STEP_HZ):
        with pytest.raises(demod.DemodError) as bad:
            demod.Demodulator("am", CAPTURE_HZ, bandwidth_hz=outside)
        # The refusal says what WOULD work: this reaches the owner via the API.
        assert str(low) in str(bad.value) and str(high) in str(bad.value)


def test_a_width_off_the_grid_is_refused():
    """100 Hz, not 1 kHz: SSB's 2.4 and NFM's 12.5 are 100 Hz multiples and neither is a
    1 kHz one, so a coarser grid would put the classic filters out of reach of the very
    control meant to offer them."""
    demod = _load()
    with pytest.raises(demod.DemodError, match="multiple"):
        demod.Demodulator("am", CAPTURE_HZ, bandwidth_hz=5_050)
    # ...and every preset is on the grid, or the ladder offers what the box refuses.
    for mode, ladder in demod.BANDWIDTH_HZ.items():
        low, high = demod.BANDWIDTH_RANGE_HZ[mode]
        for width in ladder:
            assert width % demod.BANDWIDTH_STEP_HZ == 0, (mode, width)
            assert low <= width <= high, (mode, width)


def test_every_width_on_the_grid_builds_a_real_filter():
    """The range replaced a ladder, so the guarantee has to cover the range.

    Walked at 1 kHz — the step the drag actually produces — asserting each chain builds
    and its channel filter passes its own edge. A width that raised, or that quietly
    came out attenuating its own passband, would reach the owner as a filter that
    sounds wrong at one setting and fine at the next."""
    demod = _load()
    for mode, (low, high) in demod.BANDWIDTH_RANGE_HZ.items():
        for width in range(low, high + 1, 1_000):
            built = demod.Demodulator(mode, CAPTURE_HZ, bandwidth_hz=width)
            assert built.bandwidth_hz == width, (mode, width)
            assert built.channel_half_hz <= built.crop_reach_hz, (mode, width)
            channel = built._build_channel()
            if channel is None:  # wide FM: the front end is its own channel filter
                continue
            freqs = np.fft.rfftfreq(4096, 1.0 / built.if_rate_hz)
            resp = np.abs(np.fft.rfft(channel._taps[::-1].real, 4096))
            at_edge = resp[np.argmin(np.abs(freqs - built.channel_half_hz))]
            assert 20.0 * np.log10(at_edge / resp[0]) > -6.5, (mode, width)


def test_the_picture_does_not_zoom_when_the_filter_narrows():
    """`crop_reach_hz` comes from the mode's widest filter, not the one in force.

    `listen._tuning_frame` crops the tuning strip to four times this. Deriving it from
    the live passband would zoom the picture in every time the owner narrowed the
    filter — hiding the interfering station at the exact moment they narrowed it to
    reject that station, and leaving the shaded box the same fraction of the picture at
    every setting, so the control would look like it had done nothing."""
    demod = _load()
    for mode, ladder in demod.BANDWIDTH_HZ.items():
        widest = demod.Demodulator(mode, CAPTURE_HZ, bandwidth_hz=ladder[0])
        for bandwidth_hz in ladder:
            built = demod.Demodulator(mode, CAPTURE_HZ, bandwidth_hz=bandwidth_hz)
            assert built.crop_reach_hz == widest.crop_reach_hz, mode
            # ...and the shaded box really is narrower inside that fixed picture.
            assert built.channel_half_hz <= built.crop_reach_hz, (mode, bandwidth_hz)


def test_the_default_bandwidth_preserves_every_mode_but_am():
    """The ladder's first rung reproduces the filter each mode already had.

    This wave is meant to change exactly one default — AM's, from 16 kHz to 8 — and a
    ladder is an easy place to move another one by accident."""
    demod = _load()
    for mode, half_hz in (
        ("fm", 8_000.0),
        ("nfm", 8_000.0),
        ("usb", 3_400.0),
        ("lsb", 3_400.0),
        ("wbfm", 90_000.0),
    ):
        assert demod.Demodulator(mode, CAPTURE_HZ).channel_half_hz == half_hz, mode
    assert demod.Demodulator("am", CAPTURE_HZ).channel_half_hz == 4_000.0
