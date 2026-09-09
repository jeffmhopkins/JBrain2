"""One capture, many sinks — the fanout, without a radio.

`capture.py` is where "listen to a station OR look at the band, pick one" stopped
being a property of this code. What it can get wrong is not arithmetic — `iq.py` and
`demod.py` own that and are tested for it — but the SEAM: whether every sink really
sees the same buffer, whether a row is labelled with where the samples came from or
with wherever the radio has since gone, and whether one sink failing can strand the
others' teardown. All three are silent in production and all three are testable with
a fake radio and no dongle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name: str, filename: str):
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"sdr/{filename}")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


radio = _load("sdr_radio_for_capture", "radio.py")
iq = _load("sdr_iq_for_capture", "iq.py")
demod = _load("sdr_demod_for_capture", "demod.py")
capture = _load("sdr_capture", "capture.py")

RATE = 2_400_000
CENTER = 145_000_000


class _Radio:
    """A radio that hands over the samples it is told to, and can move mid-stream."""

    def __init__(
        self, *, center_hz: int = CENTER, frames: int = 1, tone_hz: float = 0.0
    ):
        self.center_hz = center_hz
        self.rate_hz = RATE
        self.alive = True
        self.left = frames
        self.tone_hz = tone_hz
        self.wanted: list[int] = []
        self.overflows = 0
        self._phase = 0.0

    def read(self, samples: int) -> Any:
        self.wanted.append(samples)
        self.left -= 1
        if self.left < 0:
            self.alive = False
        n = int(samples)
        k = np.arange(n, dtype=np.float64) + self._phase
        self._phase += n
        wave = np.exp(2.0j * np.pi * self.tone_hz * k / RATE)
        return radio.Reading(
            samples=wave.astype(np.complex64),
            at=1234.5,
            reads=1,
            overflows=self.overflows,
            timeouts=0,
            center_hz=self.center_hz,
        )


class _Recorder(capture.Sink):
    def __init__(self, want: int = 0) -> None:
        self.want = want
        self.seen: list[Any] = []
        self.closed = 0

    def feed(self, reading: Any) -> None:
        self.seen.append(reading)

    def close(self) -> None:
        self.closed += 1


def _run(held, sinks, *, overflowed=None, frames: int = 1) -> None:
    left = [frames]

    def running() -> bool:
        left[0] -= 1
        return left[0] >= 0

    capture.Capture(held, sinks, running=running, overflowed=overflowed).run()


def test_every_sink_is_handed_the_very_same_buffer() -> None:
    """Not equal arrays — the SAME array.

    This is the whole claim the fanout makes: the picture and the sound are not two
    measurements that could disagree, they are one buffer looked at twice. Two sinks
    each doing their own `read` would satisfy any equality assertion and would be
    exactly the thing this file exists to stop."""
    one, two = _Recorder(), _Recorder()
    _run(_Radio(), [one, two])

    assert len(one.seen) == 1 and len(two.seen) == 1
    assert one.seen[0] is two.seen[0]
    assert one.seen[0].samples is two.seen[0].samples


def test_the_read_is_as_long_as_the_hungriest_sink_asked_for() -> None:
    """One read, sized for whoever needs most. A sink that wanted fewer gets more,
    which for a streaming block is a slower, better-averaged output and never an
    error."""
    held = _Radio()
    _run(held, [_Recorder(want=1024), _Recorder(want=240_000)])

    assert held.wanted == [240_000]


def test_a_capture_with_no_appetite_still_reads() -> None:
    """`want = 0` means "whatever the others need", and every sink saying it must not
    ask the radio for zero samples — `Radio.read` raises on that."""
    held = _Radio()
    _run(held, [_Recorder(), _Recorder()])

    assert held.wanted == [1]


def test_a_sink_that_raises_does_not_strand_the_others_teardown() -> None:
    """ffmpeg only flushes and exits when its stdin is CLOSED, and that close is a
    sink's `close`. One sink blowing up on a torn buffer must not leave the encoder —
    or the next sink after it — holding a handle for the life of the container."""

    class _Angry(capture.Sink):
        closed = 0

        def feed(self, reading: Any) -> None:
            raise ValueError("no")

        def close(self) -> None:
            type(self).closed += 1
            raise OSError("nor this")

    after = _Recorder()
    angry = _Angry()
    with pytest.raises(ValueError):
        _run(_Radio(), [angry, after])

    assert _Angry.closed == 1
    assert after.closed == 1
    assert after.seen == []  # the raise stopped the fanout before it reached this one


def test_overflows_are_reported_as_they_are_counted() -> None:
    """Once per reading, with the reading's own count — not a running total the caller
    would have to difference."""
    held = _Radio(frames=3)
    held.overflows = 2
    seen: list[int] = []
    _run(held, [_Recorder()], overflowed=seen.append, frames=3)

    assert seen == [2, 2, 2]


def test_a_clean_reading_says_nothing_about_overflows() -> None:
    seen: list[int] = []
    _run(_Radio(), [_Recorder()], overflowed=seen.append)

    assert seen == []


def test_the_loop_stops_when_the_radio_goes_away() -> None:
    """`alive` is the radio's own answer and it ends the capture without an exception:
    a closed device is a stopped session, not a failure to report."""
    held = _Radio(frames=2)
    sink = _Recorder()
    _run(held, [sink], frames=99)

    assert len(sink.seen) == 3  # two live frames, then the one that turned it off


def test_a_band_row_is_labelled_with_the_buffer_it_came_from() -> None:
    """A hopping stream retunes between reads, so a sink that asked the RADIO where it
    is would label every row with wherever the radio went next. The centre rides on the
    buffer for that reason and this is the test that it is used."""
    rows: list[Any] = []
    sink = capture.BandSink(iq.Spectrometer(1024, RATE), row=rows.append)
    held = _Radio(frames=2)
    left = [2]

    def running() -> bool:
        left[0] -= 1
        if left[0] == 0:
            held.center_hz = CENTER + 10_000_000  # the hop, between two reads
        return left[0] >= 0

    capture.Capture(held, [sink], running=running).run()

    assert len(rows) == 2
    assert rows[0].start_hz == pytest.approx(CENTER - 512 * rows[0].bin_hz)
    assert rows[1].start_hz == pytest.approx(CENTER + 10_000_000 - 512 * rows[1].bin_hz)


def test_a_band_row_shorter_than_one_segment_is_dropped_not_raised() -> None:
    """`Spectrometer.frame` needs a whole segment. A short read is the next read's
    problem, not the session's end."""
    rows: list[Any] = []
    sink = capture.BandSink(iq.Spectrometer(4096, RATE), row=rows.append, want=64)
    _run(_Radio(), [sink])

    assert rows == []


def test_a_channel_view_is_centred_on_the_station_not_on_the_radio() -> None:
    """The radio sits `offset_hz` BELOW the station and the mixer takes that back out,
    so the demodulator's baseband is about the station. Deriving that from the buffer
    rather than from the session's idea of its own frequency is what keeps the two from
    drifting apart across a retune."""
    station = CENTER
    chain = demod.Demodulator("fm", RATE, offset_hz=300_000.0)
    seen: list[Any] = []
    sink = capture.ChannelSink(
        chain,
        audio=lambda out: None,
        view=lambda spectrum, passband, reach: seen.append((spectrum, passband, reach)),
        view_bins=512,
        want=RATE // 10,
    )
    held = _Radio(center_hz=station - int(chain.offset_hz), frames=4, tone_hz=0.0)
    _run(held, [sink], frames=4)

    assert seen, "the channel sink drew nothing"
    spectrum, passband, reach = seen[0]
    middle = spectrum.start_hz + spectrum.bins / 2 * spectrum.bin_hz
    assert middle == pytest.approx(station, abs=spectrum.bin_hz)
    # The PASSBAND as two edges, not a half-width doubled: on SSB those are not the same
    # thing, and this sink used to hand over the symmetric one (C14).
    assert passband == chain.passband_hz == (-8_000.0, 8_000.0)
    # ...and the crop reach ALONGSIDE it rather than derived from it, so narrowing the
    # filter shrinks the shaded box without zooming the picture in on it.
    assert reach == chain.crop_reach_hz == 8_000.0


def test_a_channel_sink_that_draws_nothing_still_makes_audio() -> None:
    """The view is optional — a second VFO recording a channel nobody is watching is
    the shape A4 wants — and asking for no view must not cost the audio."""
    chain = demod.Demodulator("fm", RATE, offset_hz=300_000.0)
    heard: list[Any] = []
    sink = capture.ChannelSink(chain, audio=heard.append, want=RATE // 10)
    _run(_Radio(center_hz=CENTER - 300_000, frames=2), [sink], frames=2)

    assert sink.bins == 0
    assert heard and heard[0].pcm.size


def test_a_channel_sink_closes_by_finishing_its_audio() -> None:
    """ffmpeg's stdin, closed on the capture thread when the loop ends."""
    chain = demod.Demodulator("fm", RATE, offset_hz=300_000.0)
    finished: list[bool] = []
    sink = capture.ChannelSink(
        chain, audio=lambda out: None, want=1024, finish=lambda: finished.append(True)
    )
    _run(_Radio(), [sink])

    assert finished == [True]


def test_a_band_sink_nobody_is_watching_does_no_work() -> None:
    """~11% of one core, on a session whose job is audio. Asking before transforming is
    what makes the second picture free when it is not being looked at — the transform
    is the cost, not the publish, so the question has to come first."""
    rows: list[Any] = []
    watching = [False]
    sink = capture.BandSink(
        iq.Spectrometer(1024, RATE), row=rows.append, active=lambda: watching[0]
    )
    _run(_Radio(frames=3), [sink], frames=3)
    assert rows == []

    watching[0] = True
    _run(_Radio(frames=3), [sink], frames=3)
    assert len(rows) == 3


class _SwapsOnce(_Recorder):
    """A sink that asks its capture to move, from inside the fanout.

    Deterministic where a thread would be a race, and it is also the real shape: a
    request thread calls `swap` while the pump is somewhere in its cycle."""

    def __init__(self, cap_box: list[Any], sinks: list[Any], apply=None, want: int = 0):
        super().__init__(want)
        self._box = cap_box
        self._sinks = sinks
        self._apply = apply

    def feed(self, reading: Any) -> None:
        super().feed(reading)
        if len(self.seen) == 1:
            self._box[0].swap(self._sinks, self._apply)


def test_a_swap_moves_the_radio_before_any_new_sink_sees_a_reading() -> None:
    """The ordering the whole of A2 rests on: no chain is ever handed samples from a
    frequency it was not built for. `apply` runs first, the buffer that straddled the
    move is dropped, and only then does the new sink start reading."""
    held = _Radio(frames=6)
    moved: list[int] = []
    new = _Recorder()

    def move() -> None:
        held.center_hz = CENTER + 1_000_000
        moved.append(held.center_hz)

    box: list[Any] = [None]
    old = _SwapsOnce(box, [new], move)
    box[0] = capture.Capture(held, [old], running=lambda: True)
    box[0].run()

    assert moved == [CENTER + 1_000_000]
    assert len(old.seen) == 1
    assert new.seen, "the new sink never ran"
    assert all(r.center_hz == CENTER + 1_000_000 for r in new.seen)
    assert all(r.center_hz == CENTER for r in old.seen)


def test_a_swap_drops_the_buffer_that_straddled_it() -> None:
    """`Radio.read` assembles a frame from several `readStream` calls, so the buffer in
    flight when the radio moves is half one frequency and half the other — and it is
    labelled with the one it started on. One dropped frame is 100 ms against the ~600 ms
    a pipeline rebuild costs."""
    held = _Radio(frames=9)
    sink = _Recorder()
    cap = capture.Capture(held, [sink], running=lambda: True)
    cap.swap([sink])  # armed before the loop starts, so the very first read is dropped

    cap.run()

    assert len(sink.seen) == 9  # ten readings taken, the first thrown away


def test_a_swap_does_not_close_the_sinks_it_replaces() -> None:
    """On a retune the encoder the old sink writes to is the SAME encoder, and closing
    its stdin is what ends the audio. Only the capture ending closes anything."""
    old, new = _Recorder(), _Recorder()
    held = _Radio(frames=3)
    cap = capture.Capture(held, [old], running=lambda: True)
    cap.swap([new])
    assert old.closed == 0

    cap.run()

    assert new.closed == 1
    assert old.closed == 0  # it is not in the list any more; nothing to close


def test_a_swap_resizes_the_read() -> None:
    """A `want` captured once would size every later read for a chain that is gone —
    on a mode change that is a different demodulator with a different appetite."""
    held = _Radio(frames=4)
    box: list[Any] = [None]
    box[0] = capture.Capture(
        held,
        [_SwapsOnce(box, [_Recorder(want=240_000)], want=4096)],
        running=lambda: True,
    )

    box[0].run()

    assert held.wanted[0] == 4096
    assert held.wanted[-1] == 240_000
