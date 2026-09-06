"""One capture, many sinks — the shape every other SDR application already has.

A radio produces ONE stream of samples. What a receiver does with them — demodulate
a channel, transform the band, decode packets, record — is a set of consumers hanging
off that one stream, not a mode the radio is put into. gqrx runs a receiver chain and
an FFT tap off a single source; SDR++ hangs several VFOs off one; GNU Radio makes the
fanout an explicit block; OpenWebRX gives every connected client its own chain over
the same capture.

`listen.py` had this fanout already — one `read` feeding a demodulator and a
spectrometer — but hardcoded inside one method for exactly one pair, which is why
"listen to a station OR look at the band, pick one" was a property of this code
rather than of the hardware (`docs/plans/SDR_RECEIVER_CONVERGENCE_PLAN.md` A1/A3).
The radio was capturing 2.4 MHz either way and throwing all but 32 kHz of it away.

**Nothing in this file knows what a session is.** Sinks take callbacks and emit
`iq.Spectrum` and `demod.Audio`; the `listen.Frame`s, the subscriber sets and the
lifecycle stay one layer up. That is what makes a second VFO (A4) a list entry
rather than a new pipeline.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import demod
import iq

if TYPE_CHECKING:  # pragma: no cover - typing only
    import radio


class Sink:
    """One consumer of a capture's samples.

    `want` is how many samples this sink would LIKE per `feed`. The capture reads the
    LARGEST of its sinks' wants and hands every one of them the same buffer — which is
    the property the whole exercise exists for: two sinks cannot disagree about what
    was on the air, because they are not two measurements. Every sink here streams, so
    a buffer longer than it asked for is a slower, better-averaged output rather than
    an error.

    `feed` MUST NOT modify the buffer it is given: the next sink in the list sees the
    same array."""

    #: Samples per feed this sink would prefer. Zero means "whatever the others need".
    want = 0

    def feed(self, reading: "radio.Reading") -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release whatever this sink owns. Called once, on the capture thread."""


class Capture:
    """One radio, read in a loop, offered to every sink in turn.

    The loop is deliberately the ONLY thing here: no interval, no sleep, no frame
    dropping. `read` blocks until the samples exist, so the rate is the capture's own
    and the radio never looks away (`iq.py`). A read that fails RAISES — the caller
    ends the session, because a receiver that keeps emitting from a stopped stream is
    silence presented as a working radio."""

    def __init__(
        self,
        held: "radio.Radio",
        sinks: list[Sink],
        *,
        running: Callable[[], bool],
        overflowed: Callable[[int], None] | None = None,
    ) -> None:
        self.held = held
        self.sinks = list(sinks)
        self._running = running
        self._overflowed = overflowed
        # Held across the fanout, and taken by `swap`. The fanout is the only place the
        # sink list is read, and it is short — a few milliseconds against the ~100 ms
        # the loop spends inside `read` — so a caller moving the radio waits for at most
        # one frame's worth of transform.
        self._lock = threading.Lock()
        self._settling = False

    @property
    def want(self) -> int:
        """Samples per read: the largest any sink asked for, and never zero."""
        return max([sink.want for sink in self.sinks] + [1])

    def swap(self, sinks: list[Sink], apply: Callable[[], None] | None = None) -> None:
        """Move the radio and replace the sinks, without stopping the capture.

        `apply` runs before any NEW sink has seen a reading, which is the property that
        matters: no chain is ever handed samples from a frequency it was not built for.
        An OLD sink may still be finishing the buffer it was given, and that is correct —
        those samples really are from where it thinks they are.

        The old sinks are NOT closed: on a retune the encoder they write to is the same
        encoder, and closing its stdin is what ends the audio.

        **The two locks never invert.** `apply` runs under this one and takes the
        radio's `_io_lock` inside `retune`; the loop takes the radio's lock inside
        `read` and this one only AFTER that read has fully returned. So no thread ever
        holds one while waiting for the other. What the two do contend for is the
        driver: the retune's settle discards through `read_into` while the pump may be
        mid-frame, so some of the stale samples end up in the pump's buffer instead of
        the barrier's — which is precisely the buffer `_settling` throws away.

        The next reading is DROPPED. `Radio.read` assembles a frame from several
        `readStream` calls and `_io_lock` only stops a retune landing inside one of
        them — so the buffer in flight when this returns straddles two frequencies, and
        it is labelled with the one it started on.

        MEASURED ON AIR 2026-09-06 (`listen-probe --retune-to`), two bands and both mode
        families: **231-235 ms worst gap against a 103 ms median** — one dropped frame
        plus the 30 ms settle — with `stream_rebuilt: false` every time. What it
        replaces reopened the device, which `listen.Session.alive` measures at about
        half a second on this box before ffmpeg is even relaunched: the difference
        between a click and a gap."""
        with self._lock:
            if apply is not None:
                apply()
            self.sinks = list(sinks)
            self._settling = True

    def run(self) -> None:
        """Read until the caller says stop or the radio goes away."""
        try:
            while self._running() and self.held.alive:
                # Re-read every turn rather than once: `swap` can change what the sinks
                # want, and a `want` captured before it would size every later read for
                # a chain that is gone.
                reading = self.held.read(self.want)
                if reading.overflows and self._overflowed is not None:
                    self._overflowed(reading.overflows)
                with self._lock:
                    if self._settling:
                        # The buffer that straddled the retune. Dropped rather than fed
                        # to sinks that would draw it at the frequency it started on.
                        self._settling = False
                        continue
                    sinks = self.sinks
                for sink in sinks:
                    sink.feed(reading)
        finally:
            # Every sink, even after one of them raised: the encoder's stdin has to be
            # closed for ffmpeg to flush and exit, and a sink that owns nothing has a
            # `close` that does nothing. Suppressed per sink so the first failure cannot
            # strand the rest.
            for sink in self.sinks:
                with contextlib.suppress(Exception):
                    sink.close()


class ChannelSink(Sink):
    """One VFO: a demodulator, its audio, and the picture of the channel it hears.

    The view is taken from the demodulator's OWN `baseband` rather than from the
    capture, and that is what makes it a picture of the channel rather than a crop of
    the band: it is the signal after the mixer and the front-end decimation, at
    `view_rate_hz`, so 512 bins land 93.75 Hz apart instead of the 600 Hz a
    whole-capture transform of the same length would give (`listen.TUNING_BINS`).

    A second instance at a different `offset_hz` off the same capture is a second
    receiver for no extra USB bandwidth (A4). Nothing here is per-session."""

    def __init__(
        self,
        chain: demod.Demodulator,
        *,
        audio: Callable[[demod.Audio], None],
        view: Callable[[iq.Spectrum, tuple[float, float]], None] | None = None,
        view_bins: int = 0,
        want: int = 0,
        finish: Callable[[], None] | None = None,
    ) -> None:
        self.chain = chain
        self._audio = audio
        self._view = view
        self._finish = finish
        self._spectrometer = (
            # `view_rate_hz`, not `if_rate_hz`: on wide FM the picture is drawn at twice
            # the rate the audio is demodulated at, so the row has spectrum either side
            # of a 180 kHz station to measure it against (`demod.VIEW_MARGIN`).
            iq.Spectrometer(view_bins, int(chain.view_rate_hz))
            if view is not None and view_bins > 0
            else None
        )
        self.want = want or max(view_bins, 1)

    @property
    def bins(self) -> int:
        """Bins in the channel picture, or zero when this VFO draws nothing."""
        return self._spectrometer.n if self._spectrometer is not None else 0

    def feed(self, reading: "radio.Reading") -> None:
        out = self.chain.feed(reading.samples)
        if out.pcm.size:
            self._audio(out)
        spectrometer = self._spectrometer
        if spectrometer is None or self._view is None:
            return
        if out.baseband.size < spectrometer.n:
            return
        # DERIVED from the buffer, not from the session's idea of where it is tuned. The
        # radio sits `offset_hz` below the station and the mixer takes that back out, so
        # what the demodulator hands over is centred on the station — wherever the radio
        # actually was when these samples were collected.
        # The PASSBAND, as (low, high) offsets, not a half-width doubled. This passed
        # `2 * channel_half_hz` and `channel_half_hz` is symmetric, so SSB's shading
        # covered ±3400 while the demodulator heard +300..+3400 — half the shaded box
        # was the sideband the back end rejects, and someone centring a signal in it put
        # half the signal where nothing can hear it (C14).
        self._view(
            spectrometer.frame(
                out.baseband,
                reading.center_hz + int(round(self.chain.offset_hz)),
                at=reading.at,
            ),
            self.chain.passband_hz,
        )

    def close(self) -> None:
        if self._finish is not None:
            self._finish()


class BandSink(Sink):
    """The whole capture, transformed: one waterfall row of everything the radio sees.

    The samples are already in memory and already paid for over USB, so this is an FFT
    and nothing else — but "nothing else" is not free. MEASURED on this container,
    2.4 MS/s into 4096 bins at 10 fps, Welch with 50% overlap: **9.3 ms of transform
    and 2.1 ms of `peaks.find` per 100 ms frame, so ~11% of one core.** (4096 is the
    cheapest useful width, not a compromise: fewer bins means MORE segments to average
    and costs more — 1024 bins is 13.9%.)

    `active` is what makes that affordable on a session whose job is audio. A band row
    nobody has subscribed to is work with no reader, so the sink asks before it does
    any: a listening session pays the 11% while someone is watching the band and
    nothing at all when they are not. A spectrum session passes no predicate, because
    drawing is the entire reason it holds the radio."""

    def __init__(
        self,
        spectrometer: iq.Spectrometer,
        *,
        row: Callable[[iq.Spectrum], None],
        want: int = 0,
        active: Callable[[], bool] | None = None,
    ) -> None:
        self._spectrometer = spectrometer
        self._row = row
        self._active = active
        self.want = want or spectrometer.n

    @property
    def bins(self) -> int:
        return self._spectrometer.n

    def feed(self, reading: "radio.Reading") -> None:
        if self._active is not None and not self._active():
            return
        # A short buffer is a dropped row, not an error: `Spectrometer.frame` needs at
        # least one whole segment and the next read is a fresh chance.
        if reading.samples.size < self._spectrometer.n:
            return
        self._row(
            self._spectrometer.frame(reading.samples, reading.center_hz, at=reading.at)
        )
