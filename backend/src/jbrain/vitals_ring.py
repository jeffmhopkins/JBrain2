"""A rolling in-process record of the box's GPU load, so the vitals graph has a past.

The graph's 1/5/15-minute windows were fed entirely by the PWA's own copy of the 1 Hz
stream, which meant a reload started the plot empty — you could not open the screen to
see the spike you had just felt. This samples the same gauge server-side, so the history
is there the moment the screen opens, on any device.

IN MEMORY, not the metrics tables. `app.host_metrics` already stores the box's vitals,
but at a 30-second cadence sized for a 30-day graph: a 1-minute window read from it is
two points. Writing a row a second to serve a fifteen-minute view would multiply that
table thirty-fold to answer a question that stops mattering a quarter of an hour later.
A restart loses the ring, and the screen simply starts filling again — the same
behaviour it already had, minus the reload.

Only GPU is sampled here. Tokens/sec is measured in the browser off the chat stream
(agent/tokenMeter.ts), so it stays session-local and its trace still starts empty after
a reload; the surface says so rather than implying the gap is a quiet box.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

import structlog

from jbrain.host_metrics import read_gpu_busy_percent

log = structlog.get_logger()

# 15 minutes at one sample a second — the widest window the graph offers, and about
# 30 kB of floats. Anything older is answered by the Ops history graph instead.
RING_SAMPLES = 900

SAMPLE_SECONDS = 1.0

# How long one gauge read may take before it is abandoned as a gap. Generous next to a
# healthy read (microseconds), short next to the cadence.
READ_TIMEOUT_SECONDS = 2.0

# How stale the newest sample may be and still be served as "the reading". Several ticks,
# so an ordinary hiccup doesn't blank the meter, but far short of the point where a number
# would be a lie about the present.
LATEST_MAX_AGE_SECONDS = 5.0

# The most samples one stream frame will carry when catching a client up. A frame normally
# holds exactly one; this bounds the burst after a tick that ran late.
MAX_CATCHUP_SAMPLES = 30


def tick_for(at: float, interval: float) -> float:
    """The tick `at` belongs to: the NEAREST multiple of `interval` on the epoch clock.

    Sampling is anchored to the wall clock rather than to "one second after the last
    read finished", and this is the other half of that: the sample is stamped with the
    second it is a reading OF, not with the instant the read returned. Both halves exist
    for the same reason — the browser plots these samples into one slot per whole second,
    so a cadence that runs at 1.0s + a few ms of read time skips a slot every minute or
    so, and the trace shows a dropout where nothing was actually missed.

    NEAREST, not floor: the loop aims at the boundary and lands a few milliseconds either
    side of it, and flooring a wake-up that came in early would stamp the sample onto the
    tick already recorded."""
    return round(at / interval) * interval if interval > 0 else at


def seconds_to_next_tick(interval: float, *, now: float | None = None) -> float:
    """How long to wait for the next multiple of `interval` on the epoch clock.

    `asyncio.sleep(interval)` after doing the work makes the period `interval` PLUS the
    work, which accumulates: the emitter slips a whole tick against the wall clock every
    few dozen ticks, and the surface reading it draws that slip as a gap. Sleeping to the
    boundary instead keeps the phase, so a slow tick costs that tick and nothing after
    it."""
    if interval <= 0:
        return 0.0
    at = now if now is not None else time.time()
    return interval - (at % interval)


class VitalsRing:
    """The last `RING_SAMPLES` seconds of GPU load, oldest first.

    A sample's `gpu` is None when the gauge could not be read, which the graph draws as
    a GAP — never a zero, which would read as an idle box."""

    def __init__(self, size: int = RING_SAMPLES) -> None:
        self._samples: deque[tuple[float, float | None]] = deque(maxlen=size)

    def record(self, at: float, gpu: float | None) -> None:
        self._samples.append((at, gpu))

    def latest(
        self, *, max_age: float = LATEST_MAX_AGE_SECONDS, now: float | None = None
    ) -> float | None:
        """The newest reading, or None if there isn't a fresh one.

        This is what every GPU-reporting route in the API answers from, so that the top
        bar, the detail graph and the roster's gauge are the SAME number rather than three
        separate sysfs reads taken at three different instants — which is how they came to
        disagree on screen, one showing nothing while another showed 94%.

        The age check matters because a single source is also a single point of failure: if
        the sampler stops, serving its last value forever would turn "we stopped looking"
        into a confident, frozen reading. None means no reading, and the surfaces already
        know how to say that."""
        if not self._samples:
            return None
        at, gpu = self._samples[-1]
        if (now if now is not None else time.time()) - at > max_age:
            return None
        return gpu

    def since(self, seconds: float, *, now: float | None = None) -> list[dict[str, object]]:
        """Samples inside the trailing window, as `{at_ms, gpu}` oldest first. Epoch
        milliseconds so the browser can merge them with its own ring without a
        timezone or precision argument."""
        cutoff = (now if now is not None else time.time()) - seconds
        return [{"at_ms": int(at * 1000), "gpu": gpu} for at, gpu in self._samples if at >= cutoff]

    def newer_than(
        self, at_ms: int | None, *, limit: int = MAX_CATCHUP_SAMPLES
    ) -> list[dict[str, object]]:
        """Samples recorded strictly after `at_ms`, oldest first — what a stream client
        has not been told yet. `None` asks only for the newest.

        The stream sends THESE rather than "whatever the gauge says now" because the
        sampler and the stream are two loops on the same second: a tick that lands a few
        milliseconds either side of the other's drops a sample from the client's copy of
        the history, and the graph draws that as a dropout. Handing over everything since
        the last frame makes the client's ring a superset of what it was told, whatever
        the two loops' phase happens to be."""
        if at_ms is None:
            newest = self._samples[-1] if self._samples else None
            return [] if newest is None else [{"at_ms": int(newest[0] * 1000), "gpu": newest[1]}]
        fresh = [
            {"at_ms": int(at * 1000), "gpu": gpu}
            for at, gpu in self._samples
            if int(at * 1000) > at_ms
        ]
        return fresh[-limit:]


async def sample_loop(
    ring: VitalsRing,
    *,
    read: Callable[[], float | None] = read_gpu_busy_percent,
    interval: float = SAMPLE_SECONDS,
) -> None:
    """Fill the ring once a second for the life of the process.

    Deliberately independent of whether anyone is watching: the point of a history is
    that it was already being kept when you go looking. A read failure records a gap
    and the loop continues — a gauge that vanishes must not stop the sampler.

    ONE SAMPLE PER WALL-CLOCK SECOND, not one per second of loop. Sleeping a full
    interval after each read made the period the interval plus the read, so the sampler
    slipped a whole second against the clock every minute or two and the second it
    slipped past held no sample at all — a gap in a history nothing had actually failed
    to record. The sleep now targets the next tick and the sample is stamped with the
    tick it belongs to, so the ring holds exactly one entry per second whatever a
    given read costs."""
    while True:
        at = tick_for(time.time(), interval)
        try:
            # OFF the event loop, with a ceiling. `read` is an ordinary blocking file read
            # and normally trivial, but it goes through the amdgpu driver: a sysfs
            # attribute backed by a device under heavy allocation can stall for seconds,
            # and this loop runs for the life of the process whether or not anyone is
            # watching. Called inline, one stalled read takes every request in the API
            # down with it — the whole process is one loop. A thread plus a timeout makes
            # the worst case a single dropped sample.
            gpu = await asyncio.wait_for(asyncio.to_thread(read), READ_TIMEOUT_SECONDS)
            ring.record(at, gpu)
        except TimeoutError:
            ring.record(at, None)
            log.warning("vitals.sample_timeout", timeout_s=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            ring.record(at, None)
            log.warning("vitals.sample_failed", exc_info=True)
        # From the tick just serviced, never from "now": a read that overran its second
        # then yields at once and the loop rejoins the clock at the next whole second,
        # rather than every later sample inheriting the delay.
        await asyncio.sleep(max(0.0, at + interval - time.time()) if interval > 0 else 0.0)
