"""The sdr sidecar's device half — the ordering rules, without a radio.

`deploy/sdr/radio.py` is the only file in the sidecar that opens a device, and every
bug it exists to prevent is an ORDERING bug: a branch written after the frequency, a
retune that does not flush, a teardown that unmakes before it deactivates, a handle
that outlives the thing pointing at it. None of those need hardware to prove — they
need a device that remembers what it was asked and in what order, which is what
`_FakeDriver` is.

The two rules that are NOT orderings are here for the same reason. `bufflen` must be a
positive multiple of 512 because librtlsdr replaces a bad one silently with the exact
1.9 fps ceiling this engine exists to remove; and an open handle must be discoverable
because `/reset` fires `USBDEVFS_RESET` from this very process, and doing that under an
open usbfs fd strands the handle at a new node with no reap path
(docs/plans/SDR_IQ_SPECTRUM_PLAN.md §3, F3).

It also proves the import is really deferred: this module imports `radio` at module
scope, in an environment with no SoapySDR, and collection succeeds. That is the whole
of §6.1, and it is a test by construction rather than an assertion.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load():
    # Same loader as test_sdr_iq.py: the sidecar's modules import each other by bare
    # name off one image WORKDIR, so the directory has to be importable for `import iq`
    # inside radio.py to resolve.
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location("sdr_radio", DEPLOY / "sdr/radio.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_radio"] = module
    spec.loader.exec_module(module)
    return module


radio = _load()
listen = importlib.import_module("listen")

WHIP, WIRE = "09022796", "77192819"
CENTER = 10_000_000
RATE = 256_000


#: About -57 dBFS against the fake's unit tone — a floor a real dongle would
#: recognise, far enough under the carrier to leave every SNR assertion intact.
NOISE_AMPLITUDE = 1e-3
#: What a relocking tuner does to the output level while it settles. Well outside
#: the noise, because a transient this measurement could not see would make the
#: fake agree with any constant at all.
SETTLING_GAIN = 0.25


class _Result:
    """`readStream`'s return: a count, or a negative SoapySDR error code."""

    def __init__(self, ret: int, flags: int = 0) -> None:
        self.ret = ret
        self.flags = flags


class _FakeRange:
    """SoapySDR hands back a Range object, not a tuple."""

    def __init__(self, low: float, high: float) -> None:
        self._low = low
        self._high = high

    def minimum(self) -> float:
        return self._low

    def maximum(self) -> float:
        return self._high


class _FakeDevice:
    """A radio that remembers what it was asked, in order, and hands back a tone.

    Deliberately models the two librtlsdr behaviours that make this file necessary:
    `bufflen` is honoured only if it is a positive multiple of 512 (and silently
    replaced otherwise), and one `readStream` returns at most what is left of the USB
    buffer it is draining."""

    def __init__(self, driver: _FakeDriver, args: dict[str, str]) -> None:
        self.driver = driver
        self.args = dict(args)
        self.rate = 0.0
        self.center = 0.0
        self.settings = {radio.DIRECT_SAMP: radio.DIRECT_OFF}
        self.stream: object | None = None
        self.unmade = False
        self.bufflen = radio.DEFAULT_BUFFLEN_BYTES
        self._k = 0  # sample counter, so the tone's phase survives across reads
        self._noise = np.random.default_rng(20260905)
        # Samples of disturbed output still owed after the last retune, and how many
        # there were, so the decay can be indexed by how far through it is.
        self._settling = 0
        self._settle_total = 0
        self._last_read = time.monotonic()
        self._overflow_due = 0
        # Gain, modelled because nothing in the engine ever set it and the probe now
        # says so. Automatic is librtlsdr's own default, which is the whole point.
        self.gain_auto = driver.gain_auto
        # Not 0.0: the bottom of the range is now a finding, and this fake stands in
        # for a radio configured the way a receiver should be.
        self.gain_db = 28.0

    # --- the log ---------------------------------------------------------------
    def _say(self, *call: Any) -> None:
        self.driver.log.append(call)

    # --- SoapySDR's surface ----------------------------------------------------
    def setSampleRate(self, direction: int, channel: int, rate: float) -> None:
        self._say("setSampleRate", direction, channel, rate)
        self.rate = rate

    def getSampleRate(self, direction: int, channel: int) -> float:
        self._say("getSampleRate", direction, channel)
        return self.rate + self.driver.rate_error

    def setFrequency(self, direction: int, channel: int, name: str, hz: float) -> None:
        self._say("setFrequency", direction, channel, name, hz)
        if hz != self.center:
            self._settling = int(self.rate * self.driver.retune_settle_s)
            self._settle_total = self._settling
        self.center = hz

    def getGainMode(self, direction: int, channel: int) -> bool:
        return self.gain_auto

    def setGainMode(self, direction: int, channel: int, automatic: bool) -> None:
        self._say("setGainMode", automatic)
        self.gain_auto = bool(automatic)

    def getGain(self, direction: int, channel: int) -> float:
        return self.gain_db

    def getGainRange(self, direction: int, channel: int) -> Any:
        return _FakeRange(0.0, 49.6)

    def setGain(self, direction: int, channel: int, value: float) -> None:
        self._say("setGain", value)
        self.gain_db = float(value)

    def writeSetting(self, key: str, value: str) -> None:
        self._say("writeSetting", key, value)
        self.settings[key] = value
        if key == radio.DIRECT_SAMP:
            # `rtlsdr_set_direct_sampling` ends by re-applying the PREVIOUS mode's
            # centre. Modelled, because it is the entire reason for the ordering rule:
            # a frequency written before this one is thrown away.
            self.center = self.driver.stale_center_hz

    def readSetting(self, key: str) -> str:
        return self.settings.get(key, "")

    def getHardwareInfo(self) -> dict[str, str]:
        return {"index": str(self.driver.index_of(self.args.get("serial")))}

    def setupStream(
        self, direction: int, fmt: str, channels: list[int], args: dict[str, str]
    ) -> object:
        self._say("setupStream", direction, fmt, tuple(channels), dict(args))
        asked = int(args.get("bufflen", 0))
        honoured = asked > 0 and asked % 512 == 0 and not self.driver.ignores_bufflen
        self.bufflen = asked if honoured else radio.DEFAULT_BUFFLEN_BYTES
        self.stream = object()
        return self.stream

    def activateStream(self, stream: object) -> int:
        self._say("activateStream", stream is self.stream)
        self._k = 0
        return 0

    def deactivateStream(self, stream: object) -> int:
        self._say("deactivateStream", stream is self.stream)
        return 0

    def closeStream(self, stream: object) -> None:
        self._say("closeStream", stream is self.stream)

    def readStream(
        self,
        stream: object,
        buffs: list[Any],
        numElems: int,
        flags: int,
        timeoutUs: int,
    ) -> _Result:
        now = time.monotonic()
        if now - self._last_read > self.driver.overflow_after_s:
            # What SoapyRTLSDR does when the callback found the ring full: the buffer is
            # thrown away and the NEXT read says so, with no samples.
            self._overflow_due += 1
        self._last_read = now
        if self._overflow_due:
            self._overflow_due -= 1
            self._say("readStream", "overflow")
            return _Result(self.driver.OVERFLOW)
        elems = min(int(numElems), self.bufflen // radio.WIRE_BYTES_PER_SAMPLE)
        if self.driver.paced and self.rate:
            time.sleep(elems / self.rate)
        view = buffs[0]
        k = np.arange(self._k, self._k + elems, dtype=np.float64)
        self._k += elems
        offset = self.rate / 8.0 if self.rate else 0.0
        tone = np.exp(2.0j * np.pi * offset * k / (self.rate or 1.0))
        # A real receiver always has a noise floor, and the probe now says so when it
        # does not: a frame whose median has fallen to `iq.DB_FLOOR` is reported as
        # nothing reaching the ADC. A noiseless tone would trip that on every run, so
        # the fake grows the floor it was always missing. Seeded, so it stays a
        # fixture rather than a source of flakes.
        noise = self._noise.standard_normal(elems) + 1j * self._noise.standard_normal(
            elems
        )
        if self.driver.silent_reads > 0:
            self.driver.silent_reads -= 1
            view[:elems] = 0.0
        else:
            block = tone + NOISE_AMPLITUDE * noise
            if self._settling > 0:
                # A relocking tuner does not go quiet, it goes WRONG: the level moves,
                # which is what `_window_levels` watches for and why it can measure a
                # settle on a band with nothing on it.
                spoilt = min(self._settling, elems)
                if self.driver.settling_step:
                    # The pathological shape, and the reason it is offered: a level that
                    # is WRONG BUT STEADY defeats any rule that stops when the level
                    # stops moving, because it never moves. The adaptive discard is
                    # supposed to SAY so rather than stop early and be quick about it.
                    block[:spoilt] *= SETTLING_GAIN
                else:
                    # The ordinary shape: a settle DECAYS toward the true level. Ramped
                    # across the whole window and indexed by how much is still owed, so
                    # it stays continuous across reads of any size.
                    total = max(self._settle_total, 1)
                    done = total - self._settling
                    at = (np.arange(spoilt, dtype=np.float64) + done) / total
                    block[:spoilt] *= 1.0 + (SETTLING_GAIN - 1.0) * (1.0 - at)
                self._settling -= spoilt
            view[:elems] = block.astype(np.complex64)
        self._say("readStream", elems)
        return _Result(elems)


class _FakeDriver:
    """SoapySDR, faked at the one seam `radio.py` reaches it through."""

    RX = 1
    CF32 = "CF32"
    OVERFLOW = -4
    TIMEOUT = -1

    def __init__(
        self,
        *,
        serials: tuple[str, ...] = (WHIP, WIRE),
        rate_error: float = 0.0,
        ignores_bufflen: bool = False,
        paced: bool = False,
        overflow_after_s: float = 1e9,
        stale_center_hz: float = 1.0,
        silent_reads: int = 0,
        retune_settle_s: float = radio.SETTLE_S,
        gain_auto: bool = True,
        settling_step: bool = False,
        make_raises: Exception | None = None,
        unmake_raises: Exception | None = None,
    ) -> None:
        self.log: list[tuple[Any, ...]] = []
        self.serials = serials
        self.rate_error = rate_error
        self.ignores_bufflen = ignores_bufflen
        self.paced = paced
        self.overflow_after_s = overflow_after_s
        self.stale_center_hz = stale_center_hz
        # Reads that deliver ZEROS before the radio starts producing samples. The
        # direct-sampling branch on real hardware does this, and a probe that judged
        # one frame called a live radio deaf because of it.
        self.silent_reads = silent_reads
        # How long this radio's output is disturbed after a retune. Modelled
        # because it is the number a hopping sweep is made of, and the probe now
        # measures it: a fake with no transient at all would make the measurement
        # untestable in the one direction that matters.
        self.retune_settle_s = retune_settle_s
        # librtlsdr's own default is automatic, which is exactly the fault the probe
        # now reports — so a fake modelling a CORRECTLY configured radio must say so.
        self.gain_auto = gain_auto
        # Whether the retune transient is a STEP (wrong but steady) rather than a decay.
        # The hard case for a discard that stops when the level stops moving.
        self.settling_step = settling_step
        self.make_raises = make_raises
        self.unmake_raises = unmake_raises
        self.devices: list[_FakeDevice] = []

    def index_of(self, serial: str | None) -> int:
        return self.serials.index(serial) if serial in self.serials else -1

    def enumerate(self, args: dict[str, str]) -> list[dict[str, str]]:
        self.log.append(("enumerate", dict(args)))
        return [{"driver": "rtlsdr", "serial": s} for s in self.serials]

    def make(self, args: dict[str, str]) -> Any:
        self.log.append(("make", dict(args)))
        if self.make_raises is not None:
            raise self.make_raises
        device = _FakeDevice(self, args)
        self.devices.append(device)
        return device

    def unmake(self, device: Any) -> None:
        self.log.append(("unmake",))
        if self.unmake_raises is not None:
            raise self.unmake_raises
        device.unmade = True

    def version(self) -> str:
        return "api 0.8.0 abi 0.8 (fake)"

    def kinds(self) -> list[Any]:
        """Just the call names, for asserting on order rather than on arguments."""
        return [call[0] for call in self.log]


@pytest.fixture(autouse=True)
def _no_leaked_handles():
    """Every test starts and ends with nothing open. A handle left behind here would
    be the very leak the registry exists to make visible."""
    assert radio.holders() == {}
    yield
    assert radio.holders() == {}, "a test leaked a device handle"


# --- bufflen, which fails silently ---------------------------------------------------


def test_a_bufflen_that_is_not_a_multiple_of_512_is_refused() -> None:
    """librtlsdr does not error on one — it uses 262,144 instead, and `getStreamMTU`
    still reports what was asked for, so nothing downstream can tell. A refusal at
    startup is the only place this failure can be made visible at all."""
    for bad in (0, -512, 49_663, 1_000):
        with pytest.raises(ValueError, match="multiple of 512"):
            radio.validate_bufflen(bad)


def test_the_chosen_bufflen_is_small_enough_for_ten_frames_a_second() -> None:
    """ONE value for every rate (§3): ~100 ms per callback at the slowest rate the band
    table uses, and a fifth of that at the fastest — where it simply means more
    callbacks per frame. That is what keeps `setSampleRate` live, with no stream
    rebuild to zoom."""
    assert radio.BUFFLEN_BYTES % 512 == 0
    assert radio.BUFFLEN_BYTES < radio.DEFAULT_BUFFLEN_BYTES
    per_buffer = radio.samples_per_buffer()
    assert per_buffer / 256_000 == pytest.approx(0.097, abs=0.005)
    # And the ceiling the default imposes, which is what this replaces: 1.9 fps.
    assert radio.samples_per_buffer(radio.DEFAULT_BUFFLEN_BYTES) / 250_000 > 0.5


def test_the_bufflen_reaches_the_stream_as_a_setup_argument() -> None:
    """`bufflen` is a `getStreamArgsInfo` STREAM ARG consumed inside `setupStream`, not
    a `writeSetting` key: asking for it the other way is a no-op the driver accepts."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        setup = next(c for c in driver.log if c[0] == "setupStream")
        assert setup[4] == {"bufflen": str(radio.BUFFLEN_BYTES)}
        assert ("writeSetting", "bufflen", str(radio.BUFFLEN_BYTES)) not in driver.log
        assert rig.samples_per_buffer == radio.BUFFLEN_BYTES // 2


# --- the ordering rules --------------------------------------------------------------


def test_the_branch_is_set_before_the_frequency() -> None:
    """`rtlsdr_set_direct_sampling` ends by re-applying the PREVIOUS mode's centre
    (§6.15), so a frequency written first is thrown away by the branch change — and the
    radio then reports a centre it is not on."""
    driver = _FakeDriver()
    with radio.Radio.open(
        driver=driver, rate_hz=RATE, center_hz=CENTER, direct=True
    ) as rig:
        kinds = driver.kinds()
        assert kinds.index("writeSetting") < kinds.index("setFrequency")
        assert driver.devices[0].center == float(CENTER)
        assert driver.devices[0].settings[radio.DIRECT_SAMP] == radio.DIRECT_Q_BRANCH
        assert rig.direct is True


def test_the_frequency_uses_the_named_overload() -> None:
    """The 3-argument form distributes over `listFrequencies()` = {"RF", "CORR"}, so it
    zeroes any ppm correction and pays a second I2C write on every retune (§6.15)."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER):
        tune = next(c for c in driver.log if c[0] == "setFrequency")
        assert tune == ("setFrequency", driver.RX, 0, "RF", float(CENTER))


def test_changing_the_branch_rewrites_the_frequency_that_was_not_asked_for() -> None:
    """A caller that moves only the branch still gets a frequency write, because the
    driver just restored the old mode's centre underneath it."""
    driver = _FakeDriver(stale_center_hz=99_000_000.0)
    with radio.Radio.open(
        driver=driver, rate_hz=RATE, center_hz=CENTER, direct=False
    ) as rig:
        driver.log.clear()
        rig.retune(direct=True, settle_s=0.0)

    assert driver.kinds()[:2] == ["writeSetting", "setFrequency"]
    assert driver.devices[0].center == float(CENTER)


def test_a_retune_flushes_the_ring_and_then_discards_the_settle() -> None:
    """The barrier is TWO steps and neither is optional. `setFrequency` does not set
    `resetBuffer` — only `setSampleRate` does — so up to `numBuffers * bufflen` samples
    of the OLD band survive a retune and would be stamped with the NEW centre. Calling
    `activateStream` on a running stream is a pure FIFO flush; the settle after it is
    for the hardware pipeline `resetBuffer` cannot reach."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        driver.log.clear()
        dropped = rig.retune(center_hz=CENTER + 1_000_000, settle_s=0.1)

    kinds = driver.kinds()
    assert kinds.index("setFrequency") < kinds.index("activateStream")
    assert kinds.index("activateStream") < kinds.index("readStream")
    assert dropped == int(RATE * 0.1)


def test_a_rate_change_does_not_rebuild_the_stream() -> None:
    """The point of one small `bufflen`: there is no `setupStream` argument that tracks
    the rate, so zoom is `setSampleRate` alone and the picture never blanks (§3)."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        token = rig.stream_token
        rig.retune(rate_hz=2_400_000, settle_s=0.0)
        assert rig.stream_token == token
        assert rig.rate_hz == 2_400_000
    assert driver.kinds().count("setupStream") == 1


def test_the_achieved_rate_is_read_back_but_never_labels_a_frame() -> None:
    """librtlsdr quantises to the 28.8 MHz divider, so requested != achieved. The
    achieved figure is for reporting and validation; `rate_hz` — what `start_hz` and
    `bin_hz` are derived from — stays the requested integer, because a +-1 Hz flap in a
    derived `start_hz` re-blanks the PWA's waterfall on every frame (§6.8)."""
    driver = _FakeDriver(rate_error=0.0149)
    with radio.Radio.open(driver=driver, rate_hz=1_500_000, center_hz=CENTER) as rig:
        assert rig.rate_hz == 1_500_000
        assert rig.achieved_rate_hz == pytest.approx(1_500_000.0149)
        assert rig.rate_warning is not None
        assert "divider" in rig.rate_warning


def test_an_exact_rate_raises_no_warning() -> None:
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        assert rig.achieved_rate_hz == float(RATE)
        assert rig.rate_warning is None


# --- overflow, which used to be silent -----------------------------------------------


def test_an_overflow_is_a_count_on_the_frame_not_an_exception() -> None:
    """`readStream` returning `SOAPY_SDR_OVERFLOW` means the FIFO filled and a buffer
    was dropped, so the samples either side are not adjacent in time. `rtl_sdr` had no
    such signal at all — libusb simply stopped resubmitting — so this number is the new
    information, and it rides on the frame rather than going to a log (§3)."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        driver.devices[0]._overflow_due = 2
        reading = rig.read(4_096)

    assert reading.samples.size == 4_096
    assert reading.overflows == 2
    assert reading.torn is True


def test_a_stream_error_that_is_not_an_overflow_is_raised() -> None:
    """`SOAPY_SDR_STREAM_ERROR` is not a dropped buffer, it is a dead stream, and
    counting it would turn a session that has stopped into one that looks slow."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        device = driver.devices[0]
        device.readStream = lambda *_a, **_k: _Result(-2)  # type: ignore[method-assign]
        with pytest.raises(radio.RadioError, match="code -2"):
            rig.read(1_024)


# --- teardown, and the handle the lease cannot see ------------------------------------


def test_teardown_is_deactivate_then_close_then_unmake() -> None:
    """`~SoapyRTLSDR()` is a bare `rtlsdr_close` with no stream teardown, and
    `deactivateStream` is what joins the async reader thread. Any other order leaves a
    dongle that answers no descriptor reads — the state `/reset` exists for."""
    driver = _FakeDriver()
    rig = radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER)
    driver.log.clear()
    rig.close()

    assert driver.kinds() == ["deactivateStream", "closeStream", "unmake"]
    assert driver.devices[0].unmade is True
    assert rig.alive is False


def test_closing_twice_is_harmless() -> None:
    driver = _FakeDriver()
    rig = radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER)
    rig.close()
    driver.log.clear()
    rig.close()

    assert driver.log == []


def test_an_open_that_fails_gives_the_device_back(monkeypatch) -> None:
    """A device made and then abandoned is the leak with no reap path: a stranded child
    process is killed on the next sweep, a stranded device handle lives for the life of
    the container (§6.18)."""
    driver = _FakeDriver()

    def _boom(*_a: Any, **_k: Any) -> object:
        raise RuntimeError("no such stream")

    monkeypatch.setattr(_FakeDevice, "setupStream", _boom)
    with pytest.raises(radio.RadioError, match="could not start the stream"):
        radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER)

    assert driver.devices[0].unmade is True
    assert radio.holders() == {}


def test_a_device_that_will_not_open_says_so() -> None:
    driver = _FakeDriver(make_raises=RuntimeError("no device found"))
    with pytest.raises(radio.RadioError, match="could not open the radio"):
        radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER, serial=WHIP)


def test_a_second_handle_on_the_same_radio_is_refused() -> None:
    driver = _FakeDriver()
    with (
        radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER, serial=WHIP),
        pytest.raises(radio.RadioBusy),
    ):
        radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER, serial=WHIP)
    # ...and the refused attempt made no device, so nothing was opened and dropped.
    assert driver.kinds().count("make") == 1


def test_an_open_handle_is_discoverable_so_a_reset_cannot_fire_under_it() -> None:
    """`/reset` gates on the lease, which knows about child processes and TTL
    reservations and cannot see an in-process handle. Firing `USBDEVFS_RESET` from the
    process still holding the usbfs fd re-enumerates the device at a new node and
    strands the handle at ENODEV for ever. `holders()` is what closes that (§3)."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER, serial=WHIP):
        held = radio.holders()
        assert held == {WHIP: "reading I/Q"}
        # Asked through the SAME rule the lease uses, so the two registries cannot
        # disagree about what blocks what.
        assert listen.blocking_key(held, WHIP) == WHIP
        assert listen.blocking_key(held, WIRE) is None
    assert listen.blocking_key(radio.holders(), WHIP) is None


def test_an_unnamed_handle_blocks_every_radio() -> None:
    """A handle opened with no serial takes whichever device enumerates first, so
    nothing can prove it is not on the radio a reset is aimed at."""
    driver = _FakeDriver()
    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER):
        held = radio.holders()
        assert held == {radio.ANY_DEVICE: "reading I/Q"}
        assert listen.blocking_key(held, WHIP) == radio.ANY_DEVICE
        assert listen.blocking_key(held, WIRE) == radio.ANY_DEVICE


def test_the_unnamed_key_is_the_one_the_lease_already_uses() -> None:
    """Copied rather than imported, because `listen` imports this module at F6 and a
    cycle would be paid at import time — so the copy is pinned here instead."""
    assert radio.ANY_DEVICE == listen.ANY_DEVICE


def test_a_handle_that_will_not_close_keeps_blocking_the_reset() -> None:
    """This looks backwards until you ask what the entry is for: a handle we could not
    close is precisely the handle that is still held, and a port reset under it is the
    dangerous case. So it keeps its entry, and the refusal names the recovery that does
    work — restarting the service."""
    driver = _FakeDriver(unmake_raises=RuntimeError("device busy"))
    rig = radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER, serial=WIRE)
    with pytest.raises(radio.RadioError, match="would not close cleanly"):
        rig.close()

    held = radio.holders()
    assert "restart the sdr service" in held[WIRE]
    assert listen.blocking_key(held, WIRE) == WIRE
    # Cleaned up by hand: the point of the test is that nothing else does it.
    radio._open.clear()


# --- the probe ------------------------------------------------------------------------


def test_the_probe_answers_every_f0_question(monkeypatch) -> None:
    """One run, seven claims, and a verdict rather than a dump. The tone the fake hands
    back sits a quarter of the way up the passband, so the capture also proves the frame
    is stamped with the centre the radio is on NOW rather than the one it started at."""
    monkeypatch.setattr(radio, "PROBE_BACKPRESSURE_S", 0.02)
    monkeypatch.setattr(radio, "PROBE_CALLBACK_READS", 4)
    # Gain fixed, because this test asserts NO findings and an automatic gain is now
    # one of them — a radio configured the way a spectrum instrument needs it.
    driver = _FakeDriver(paced=True, overflow_after_s=0.01, gain_auto=False)

    out = radio.probe(
        serial=WIRE, center_hz=CENTER, rate_hz=2_400_000, bins=4_000, driver=driver
    )

    assert out["findings"] == []
    assert out["ok"] is True
    assert out["enumerate"]["count"] == 2
    assert out["selection"]["selects_by_serial"] is True
    assert out["direct_samp"]["works"] is True
    assert out["bufflen"]["took"] is True
    assert out["bufflen"]["elems_per_read"] == radio.samples_per_buffer()
    assert out["live_retune"]["works"] is True
    assert out["live_retune"]["stream_rebuilt"] is False
    assert all(row["works"] for row in out["rates"])
    assert out["overflow"]["reported"] is True
    # The tone is at rate/8 above centre, and the retune moved the centre first.
    moved = CENTER + 2_400_000
    assert out["live_retune"]["capture"]["peak_hz"] == pytest.approx(
        moved + 2_400_000 / 8, abs=600
    )
    assert out["live_retune"]["capture"]["above_floor_db"] > 20


def test_the_probe_catches_a_bufflen_that_did_not_take(monkeypatch) -> None:
    """The failure this check exists for is invisible everywhere else: no error, no
    log line, and `getStreamMTU` still reporting the value that was asked for. Only the
    callback period tells the truth."""
    monkeypatch.setattr(radio, "PROBE_BACKPRESSURE_S", 0.02)
    monkeypatch.setattr(radio, "PROBE_CALLBACK_READS", 3)
    driver = _FakeDriver(paced=True, ignores_bufflen=True, overflow_after_s=0.01)

    out = radio.probe(
        serial=WIRE, center_hz=CENTER, rate_hz=2_400_000, bins=4_000, driver=driver
    )

    assert out["bufflen"]["took"] is False
    assert out["ok"] is False
    assert any("did NOT take" in f for f in out["findings"])
    assert out["bufflen"]["elems_per_read"] == radio.samples_per_buffer(
        radio.DEFAULT_BUFFLEN_BYTES
    )


def test_the_probe_names_a_radio_that_selection_missed(monkeypatch) -> None:
    """Every per-radio claim in this system — the lease, the roles, the 409 that names
    a dongle — rests on `serial=` opening the radio it names."""
    monkeypatch.setattr(radio, "PROBE_BACKPRESSURE_S", 0.02)
    monkeypatch.setattr(radio, "PROBE_CALLBACK_READS", 2)
    driver = _FakeDriver(overflow_after_s=0.01)
    monkeypatch.setattr(_FakeDevice, "getHardwareInfo", lambda _s: {"index": "0"})

    out = radio.probe(serial=WIRE, center_hz=CENTER, rate_hz=RATE, driver=driver)

    assert out["selection"]["selects_by_serial"] is False
    assert out["ok"] is False
    assert any("did NOT open the radio it named" in f for f in out["findings"])


def test_the_probe_gives_the_radio_back_even_when_it_throws(monkeypatch) -> None:
    """It holds a real device handle, so an exception on the way through is exactly the
    path that would leave one behind.

    The throw no longer escapes — a probe that raises answers none of its seven
    questions, and MEASURED 2026-09-05 its message did not survive the edge — but the
    release is the property this test was written for and it still holds. Both are
    asserted: a verdict that cost the radio would be worse than the exception was."""
    driver = _FakeDriver()
    monkeypatch.setattr(
        _FakeDevice,
        "readStream",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("the radio fell over")),
    )

    out = radio.probe(serial=WHIP, center_hz=CENTER, rate_hz=RATE, driver=driver)

    assert out["ok"] is False
    assert "the radio fell over" in out["summary"]
    assert radio.holders() == {}
    assert driver.devices[0].unmade is True


def test_the_probe_says_so_when_nothing_is_plugged_in() -> None:
    driver = _FakeDriver(serials=())

    out = radio.probe(driver=driver)

    assert out["ok"] is False
    assert "enumerated no rtlsdr device" in out["summary"]
    assert driver.kinds() == ["enumerate"]


def test_a_probe_that_breaks_returns_a_verdict_rather_than_an_exception() -> None:
    """MEASURED 2026-09-05: the first real-hardware run raised inside `probe`, came back
    as a 502, and the sentence was replaced by the edge's own error page before it
    reached the console — so the one call shape that failed was invisible from the only
    surface the owner has (CLAUDE.md #10).

    A probe answers seven questions; one that raises answers none of them, and the
    verdict is the whole contract. Every escape is a finding now, and the traceback goes
    to the container log where `logs sdr` can reach it."""

    class _Exploding:
        def version(self) -> str:
            return "api 0.8.0 abi 0.8"

        def enumerate(self, _args):
            raise RuntimeError("SWIG says no")

    out = radio.probe(driver=_Exploding())  # type: ignore[arg-type]

    assert out["ok"] is False
    assert "SWIG says no" in out["summary"]
    assert any("SWIG says no" in f for f in out["findings"])


def test_a_driver_whose_version_call_fails_still_gets_probed() -> None:
    """`version()` is the FIRST call the probe makes, so an exception there used to take
    the whole verdict down before a single claim was tested."""

    class _NoVersion:
        def version(self) -> str:
            raise AttributeError("getAPIVersion missing")

        def enumerate(self, _args):
            return []

    out = radio.probe(driver=_NoVersion())  # type: ignore[arg-type]

    assert out["ok"] is False
    assert "unavailable" in out["soapy"] and "getAPIVersion" in out["soapy"]
    # It got PAST the version call to a real answer about enumeration.
    assert "enumerated no rtlsdr device" in out["summary"]


def test_a_registry_with_no_rtlsdr_module_is_named_as_packaging() -> None:
    """The one diagnosis whose fix is not in this repo at all.

    If `make` has no rtlsdr factory registered, enumeration cannot be answering from
    the same place, and no filter or call shape will ever help."""
    finding = radio._diagnosis_finding(
        {
            "environment": {
                "modules": ["/usr/lib/SoapySDR/modules0.8/libaudioSupport.so"]
            },
            "filters": [
                {"filter": "driver only", "enumerate": "2", "make": "no match"}
            ],
        }
    )

    assert "packaging, not code" in finding


def test_a_filter_that_opens_wins_over_every_other_reading() -> None:
    """The only outcome that names a fix in this file, so it is reported ahead of the
    contradiction it would otherwise be described as."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"]},
            "filters": [
                {"filter": "driver only", "enumerate": "2", "make": "no match"},
                {
                    "filter": "the enumeration row itself",
                    "enumerate": "1",
                    "make": "opened",
                },
            ],
        }
    )

    assert "the enumeration row itself" in finding


def test_enumerate_and_make_disagreeing_points_inside_make() -> None:
    """The measured state on the box: the driver is loaded, the args list devices, and
    the same args will not open one. Nothing this file passes is wrong."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"]},
            "filters": [
                {"filter": "driver + serial", "enumerate": "1", "make": "no match"}
            ],
        }
    )

    assert "inside make()" in finding


def test_a_diagnosis_that_enumerates_nothing_blames_the_bus() -> None:
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"]},
            "filters": [
                {"filter": "driver only", "enumerate": "0", "make": "no match"}
            ],
        }
    )

    assert "the driver or the bus" in finding


def test_an_unreadable_environment_is_not_a_packaging_verdict() -> None:
    """`modules` is a STRING when the call raised, and the old membership test would
    have read `rtl` out of an error message — or worse, out of its absence."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": "RuntimeError: listModules is not a thing here"},
            "filters": [
                {"filter": "driver + serial", "enumerate": "1", "make": "no match"}
            ],
        }
    )

    assert "packaging" not in finding
    assert "inside make()" in finding


def test_a_module_that_registered_nothing_is_the_answer_ahead_of_everything() -> None:
    """A module can be on the search path, be listed, and have registered nothing. That
    is the one state where `enumerate` and `make` can honestly disagree, so it is read
    before the contradiction it would otherwise be described as."""
    finding = radio._diagnosis_finding(
        {
            "environment": {
                "modules": ["/usr/lib/SoapySDR/modules0.8/librtlsdrSupport.so"],
                "module_errors": {
                    "/usr/lib/SoapySDR/modules0.8/librtlsdrSupport.so": "ABI mismatch",
                },
            },
            "filters": [
                {"filter": "driver + serial", "enumerate": "1", "make": "no match"}
            ],
        }
    )

    assert "registered NOTHING" in finding and "ABI mismatch" in finding


def test_another_modules_loader_error_is_not_mistaken_for_the_rtlsdr_one() -> None:
    """The audio module fails to load on a box with no sound card and always will. It
    has nothing to do with a radio that will not open."""
    finding = radio._diagnosis_finding(
        {
            "environment": {
                "modules": ["librtlsdrSupport.so", "libaudioSupport.so"],
                "module_errors": {"libaudioSupport.so": "no such device"},
            },
            "filters": [
                {"filter": "driver + serial", "enumerate": "1", "make": "no match"}
            ],
        }
    )

    assert "registered NOTHING" not in finding
    assert "inside make()" in finding


def test_failing_exactly_like_a_name_nobody_wrote_means_unregistered() -> None:
    """MEASURED against SoapySDR 0.8.1: `make() no match` is what a driver name that
    does not exist raises, while a REGISTERED driver with no hardware raises the
    driver's own sentence instead. Matching the control is therefore not a
    coincidence — it is the reading, and it beats every other one because no filter
    and no call shape can fix it."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"], "module_errors": {}},
            "filters": [
                {
                    "filter": radio.CONTROL_FILTER,
                    "enumerate": "0",
                    "make": "SoapySDR::Device::make() no match",
                },
                {
                    "filter": "driver + serial",
                    "enumerate": "1",
                    "make": "SoapySDR::Device::make() no match",
                },
            ],
        }
    )

    assert "no rtlsdr factory answered" in finding


def test_rtlsdr_failing_differently_from_the_control_is_not_that() -> None:
    """The driver's own error means it IS registered and did run, so the contradiction
    stands and the next place to look is elsewhere."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"], "module_errors": {}},
            "filters": [
                {
                    "filter": radio.CONTROL_FILTER,
                    "enumerate": "0",
                    "make": "SoapySDR::Device::make() no match",
                },
                {
                    "filter": "driver + serial",
                    "enumerate": "1",
                    "make": "rtlsdr_get_index_by_serial(09022796) - -2",
                },
            ],
        }
    )

    assert "no rtlsdr factory answered" not in finding
    assert "inside make()" in finding


def test_the_control_opening_a_device_is_never_read_as_a_verdict() -> None:
    """It cannot happen, and if it somehow did, the control has proved nothing."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"], "module_errors": {}},
            "filters": [
                {"filter": radio.CONTROL_FILTER, "enumerate": "0", "make": "opened"},
                {"filter": "driver + serial", "enumerate": "1", "make": "opened"},
            ],
        }
    )

    assert "no rtlsdr factory answered" not in finding
    assert "DID open it" in finding


def test_the_linked_library_reading_names_real_mapped_objects() -> None:
    """It reads this process's own map, so it works anywhere and needs no SoapySDR —
    which is the point: the reading has to survive the very fault it is hunting."""
    mapped = radio._linked_soapy()

    assert isinstance(mapped, list)
    assert all(isinstance(path, str) for path in mapped)
    # No SoapySDR here, so nothing matches — and an empty answer is an answer.
    assert not any("unreadable" in path for path in mapped)


def test_asking_the_cxx_tool_survives_it_not_being_there() -> None:
    """This runs in an environment with no SoapySDRUtil, which is the case the reading
    has to survive: a diagnosis that raises while diagnosing is worse than none."""
    said = radio._soapy_util("--find=driver=rtlsdr")

    assert isinstance(said, str) and said
    # Either the tool is absent (an OSError, named) or it ran and reported.
    assert said.startswith("rc=") or "Error" in said


def test_the_cxx_tool_is_never_invoked_through_a_shell() -> None:
    """It runs as root beside the radio, so argv stays a list and the path stays
    ours — never a string a shell would re-read."""
    seen: dict[str, Any] = {}

    def _fake_run(argv: list[str], **kwargs: Any) -> None:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        raise OSError("not here")

    original = radio.subprocess.run
    radio.subprocess.run = _fake_run  # type: ignore[assignment]
    try:
        radio._soapy_util("--find=driver=rtlsdr")
    finally:
        radio.subprocess.run = original  # type: ignore[assignment]

    assert seen["argv"] == [radio.SOAPY_UTIL, "--find=driver=rtlsdr"]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["timeout"] == radio.UTIL_TIMEOUT_S


class _RecordingSdr:
    """Just enough SoapySDR to see WHAT `_Soapy.make` hands the binding."""

    def __init__(self) -> None:
        self.asked: list[object] = []

    def Device(self, args: object) -> str:
        self.asked.append(args)
        return "device"


def test_make_opens_by_args_string_not_by_dict() -> None:
    """MEASURED on the box with two dongles attached and the module registered:

        Device({"driver": "rtlsdr", "serial": "09022796"})   make() no match
        Device("driver=rtlsdr,serial=09022796")              OPENED

    A dict carrying a `driver` key is what fails, and only inside `make` — the same
    dict enumerates both devices. This is the one call in the live path, so it is the
    one that must not regress to a dict."""
    soapy = radio._Soapy.__new__(radio._Soapy)
    soapy._sdr = _RecordingSdr()  # type: ignore[assignment]

    soapy.make({"driver": "rtlsdr", "serial": "09022796"})

    assert soapy._sdr.asked == ["driver=rtlsdr,serial=09022796"]  # type: ignore[union-attr]


def test_a_loader_result_naming_a_registered_driver_is_not_an_error() -> None:
    """MEASURED and corrected: `getLoaderResult` maps each driver the module registered
    to THAT DRIVER'S error, and a registered driver's error is empty. The box returned
    `{"rtlsdr": ""}` for a module that had just registered rtlsdr perfectly well, and
    reading the keys reported a success as a failure — in the headline finding."""

    class _Sdr:
        def listModules(self) -> list[str]:
            return ["librtlsdrSupport.so", "libaudioSupport.so"]

        def getLoaderResult(self, path: str) -> dict[str, str]:
            return {"rtlsdr": ""} if "rtl" in path else {"audio": "no such device"}

    soapy = radio._Soapy.__new__(radio._Soapy)
    soapy._sdr = _Sdr()  # type: ignore[assignment]

    assert soapy._module_errors() == {"libaudioSupport.so": "audio: no such device"}


def test_an_open_that_happened_outranks_the_control_reading() -> None:
    """MEASURED: the control-matches reading fired on a box where the driver WAS
    registered and two other filters opened, and it was flatly false. A filter that
    works is a fix; "no factory registered" was a story told about the evidence."""
    finding = radio._diagnosis_finding(
        {
            "environment": {"modules": ["librtlsdrSupport.so"], "module_errors": {}},
            "filters": [
                {"filter": "no filter at all", "enumerate": "2", "make": "opened"},
                {
                    "filter": radio.CONTROL_FILTER,
                    "enumerate": "0",
                    "make": "SoapySDR::Device::make() no match",
                },
                {
                    "filter": "driver + serial",
                    "enumerate": "1",
                    "make": "SoapySDR::Device::make() no match",
                },
            ],
        }
    )

    assert finding == "A filter that DID open it: no filter at all."


def _flat_db(bins: int, value: float) -> Any:
    return np.full(bins, value, dtype=np.float64)


def test_the_dc_spike_is_never_reported_as_the_signal() -> None:
    """MEASURED on the box at 5.0, 7.15 and 10.0 MHz under direct_samp=2: the frame was
    a DC delta and nothing else, and a plain argmax called it a 203 dB signal three
    times without a murmur. Every direct-conversion receiver has that spike; a peak
    sitting on the tuned bin is the receiver looking at itself."""
    bins = 8
    db = _flat_db(bins, -200.0)
    db[bins // 2] = 3.0
    spectrum = radio.iq.Spectrum(
        at=0.0, start_hz=1_000_000, bin_hz=250, db=db, segments=8
    )

    verdict = radio._reading_verdict(spectrum)

    assert verdict["dc_db"] == 3.0
    assert verdict["peak_db"] == -200.0
    assert verdict["peak_hz"] != 1_000_000 + (bins // 2) * 250
    assert verdict["dead"] is True


def test_a_live_frame_keeps_its_carrier_and_is_not_dead() -> None:
    """The 99.3 MHz shape: a real floor with a carrier standing over it, and a DC spike
    beside them that must not win."""
    bins = 8
    db = _flat_db(bins, -44.0)
    db[bins // 2] = 3.0
    db[2] = -15.6
    spectrum = radio.iq.Spectrum(
        at=0.0, start_hz=1_000_000, bin_hz=250, db=db, segments=8
    )

    verdict = radio._reading_verdict(spectrum)

    assert verdict["peak_db"] == -15.6
    assert verdict["peak_hz"] == 1_000_000 + 2 * 250
    assert verdict["floor_db"] == -44.0
    assert verdict["dead"] is False


def test_a_peak_above_the_dc_bin_keeps_its_own_frequency() -> None:
    """`np.delete` closes the gap, so every bin past the removed one shifts down and a
    naive index would report the wrong frequency — quietly, and only on one half."""
    bins = 8
    db = _flat_db(bins, -44.0)
    db[bins // 2] = 3.0
    db[6] = -10.0
    spectrum = radio.iq.Spectrum(
        at=0.0, start_hz=1_000_000, bin_hz=250, db=db, segments=8
    )

    verdict = radio._reading_verdict(spectrum)

    assert verdict["peak_db"] == -10.0
    assert verdict["peak_hz"] == 1_000_000 + 6 * 250


def test_a_radio_that_needs_a_moment_is_not_reported_deaf() -> None:
    """MEASURED on the box: a single frame taken shortly after the direct-sampling
    branch was switched reported a dead radio that the STREAMING engine, on the same
    dongle at the same frequency and rate, found perfectly alive — a -51.5 dBFS floor
    with a peak over it. Both cannot be true of the radio, so one was true of the
    reading, and the single frame is the weaker one.

    The verdict now comes from the last of several frames, and `settled` records that
    the first had nothing in it. Before this the probe called a live radio deaf and
    said so about the owner's antenna."""
    # More silence than the opening barrier absorbs. The barrier discards 150 ms now
    # rather than 50, so a silent start short enough to fall inside it never reaches
    # `_capture` at all — which would leave this regression test asserting nothing.
    driver = _FakeDriver(silent_reads=4)

    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        verdict = radio._capture(rig, 64)

    assert verdict["dead"] is False
    assert verdict["settled"] is True
    assert verdict["frames"] == radio.PROBE_CAPTURE_FRAMES


def test_a_radio_that_is_silent_throughout_is_still_reported_dead() -> None:
    """The flag must not become an excuse. A receiver with no noise floor after ten
    frames is not settling, and saying otherwise would hide the very thing this check
    exists to find."""
    driver = _FakeDriver(silent_reads=10_000)

    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        verdict = radio._capture(rig, 64)

    assert verdict["dead"] is True
    assert verdict["settled"] is False


def test_the_settle_is_measured_against_what_the_radio_actually_needs() -> None:
    """`SETTLE_S` was chosen and never measured, and it is the whole cost of a hopping
    sweep: the samples in a hop are microseconds and the discard is milliseconds, so
    this one number decides whether a wide band redraws twice a second or twenty
    times."""
    driver = _FakeDriver(retune_settle_s=0.05)

    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        settle = radio._settle_after_retune(rig, CENTER + RATE)

    assert settle["measured"] is True
    # Within one slice of the transient the fake really has — the resolution this
    # method has, which is what its window length buys.
    assert settle["settle_ms"] == pytest.approx(50.0, abs=settle["window_us"] / 1000.0)


def test_a_radio_that_settles_far_faster_says_the_constant_is_costing_speed() -> None:
    """The finding that matters for a hopping sweep: every hop pays the difference."""
    findings = radio._settle_findings(
        {"settle_ms": 2.0, "worst_ms": 3.0, "configured_ms": 50.0, "window_us": 213.0}
    )

    assert findings and "every hop pays the difference" in findings[0]


def test_a_radio_that_needs_longer_is_a_correctness_fault_not_a_speed_one() -> None:
    """Too short is worse than too long, and the sentence has to say why: the first
    samples of a hop are then the PREVIOUS hop's, drawn at a frequency they are not on.
    A waterfall built that way is confidently wrong rather than merely slow."""
    findings = radio._settle_findings(
        {"settle_ms": 70.0, "worst_ms": 80.0, "configured_ms": 50.0, "window_us": 213.0}
    )

    assert findings and "wrong frequency" in findings[0]


def test_a_settle_that_agrees_within_a_slice_is_not_a_finding() -> None:
    """The resolution this method has is one window. Reporting a disagreement smaller
    than that would be reporting the method rather than the radio."""
    assert (
        radio._settle_findings(
            {
                "settle_ms": 50.1,
                "worst_ms": 50.13,
                "configured_ms": 50.0,
                "window_us": 213.0,
            }
        )
        == []
    )


def test_one_wild_slice_is_not_mistaken_for_a_transient() -> None:
    """Three sigma over several hundred slices puts a handful past the line by chance,
    and taking the last of those reported a whole 62 ms block as a transient — measured
    off a fake that had none. The running median is what makes the reading survive its
    own statistics."""
    levels = np.zeros(200)
    levels[150] = 99.0  # one outlier, late, exactly where it did the most damage

    smoothed = radio._smooth(levels, radio.SETTLE_SMOOTH)

    assert float(np.max(np.abs(smoothed))) == 0.0


def test_a_settle_longer_than_the_span_is_reported_as_unknown_not_as_the_span() -> None:
    """MEASURED on the box, 2026-09-05: an 80 ms span reported "80.0 ms at worst" on the
    tuner path, and 80.0 was the span. A stopwatch shorter than the event it times
    cannot time it, and a reading equal to its own window is not a measurement — saying
    so is the difference between a number and an artefact of the method."""
    findings = radio._settle_findings(
        {
            "settle_ms": 80.0,
            "worst_ms": 80.0,
            "configured_ms": 50.0,
            "window_us": 213.0,
            "span_ms": 80.0,
            "saturated": 3,
            "trials": 7,
        }
    )

    assert findings and "AT LEAST" in findings[0]
    assert "still unknown" in findings[0]


def test_one_late_excursion_does_not_report_the_whole_block_as_a_transient() -> None:
    """The rule that replaced "one past the last slice outside tolerance". Over a span
    long enough to hold the transient, something late always wanders out — a station
    fading, a gain step — and the old rule read every one of those as the radio still
    settling, which is how a reading saturates at its own span."""
    off = np.zeros(400, dtype=bool)
    off[:20] = True  # the real transient, at the start where a transient lives
    off[300] = True  # drift, long after the radio settled

    assert radio._settled_index(off, hold=40) == 20


def test_a_block_that_never_goes_quiet_has_no_settled_index() -> None:
    """None rather than a number, so the caller reports "at least the span"."""
    assert radio._settled_index(np.ones(400, dtype=bool), hold=40) is None


def test_the_hop_cost_says_which_of_two_opposite_fixes_the_discard_calls_for() -> None:
    """The FM dial hops eleven times a row and manages 1.0 fps, so a hop costs ~90 ms
    while the samples it keeps are 0.4 ms of signal. Whether the discard is bounded by
    real time or is a memcpy decides the fix — discard longer, or have less stale data
    — and the two are opposite, so it is measured rather than assumed."""
    driver = _FakeDriver(retune_settle_s=0.05)

    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        cost = radio._hop_cost(rig, CENTER + RATE, want=1024)

    assert cost["hops"] == radio.HOP_COST_HOPS * len(radio.HOP_COST_SETTLES)
    # A ladder, because one settle cannot distinguish the two: the bare flush is the
    # fixed cost and the rest is what a discard adds on top of it.
    assert set(cost["barrier_ms"]) == {"0", "50", "150"}
    # Not pinned to either answer here. Wall-clock timing against a fake measures the
    # fake, and the classification itself is tested on the pure function below.
    assert cost["bound"] in {"real-time", "memcpy"}


def test_a_discard_that_costs_nothing_reads_as_a_memcpy_not_as_real_time() -> None:
    """The other answer, and the one that would let the settle cover the worst case for
    free. Kept honest by a reading rather than by which outcome would be convenient."""
    assert radio._hop_bound({"0": 0.1, "50": 0.4, "150": 0.5}) == "memcpy"
    assert radio._hop_bound({"0": 1.2, "50": 51.0, "150": 152.0}) == "real-time"


def test_the_hop_cost_is_a_reading_not_a_finding() -> None:
    """Both answers describe a healthy radio. `findings` means a claim did not hold, and
    putting an always-emitted sentence in it made every clean probe run look failed."""
    driver = _FakeDriver(retune_settle_s=0.05)

    out = radio.probe(driver=driver)

    assert out["hop_cost"]["bound"] in {"real-time", "memcpy"}
    assert not [say for say in out["findings"] if "wall clock" in say]


def test_an_automatic_gain_is_a_finding_on_a_spectrum_instrument() -> None:
    """Nothing in this engine ever set the gain, so it runs at librtlsdr's default,
    which is automatic. A waterfall whose gain moves has a dB scale that means nothing
    from row to row, and on a hopped band every seam becomes a gain step drawn as if
    the band itself had changed."""
    out = radio.probe(driver=_FakeDriver(gain_auto=True))

    assert out["gain"]["automatic"] is True
    assert [say for say in out["findings"] if "gain is AUTOMATIC" in say]


def test_the_settle_is_asked_again_with_the_gain_nailed_down() -> None:
    """What the stopwatch watches is a LEVEL, and a gain loop moves level for exactly
    the duration measured on the box — 61 ms, where an R820T2's PLL locks in well under
    a millisecond. The A/B is what tells a relock from a re-convergence, and the probe
    must hand the radio back configured as it found it either way."""
    driver = _FakeDriver(gain_auto=True)

    out = radio.probe(driver=driver)

    assert out["retune_settle_fixed_gain"]["was_automatic"] is True
    # Given back, not left fixed: the next reading would otherwise be of our own change.
    assert out["gain"]["automatic"] is True


def test_a_gain_pinned_to_the_bottom_of_its_range_is_a_finding() -> None:
    """MEASURED on the box: manual mode at 0.0 dB, the floor of a 0-49.6 dB range.
    Nothing in this engine sets the gain, so that is a default rather than a choice,
    and it costs every weak signal on every band."""
    findings = radio._gain_findings(
        {"automatic": False, "gain_db": 0.0, "gain_min_db": 0.0, "gain_max_db": 49.6}
    )

    assert findings and "BOTTOM of its" in findings[0]
    assert (
        radio._gain_findings(
            {
                "automatic": False,
                "gain_db": 28.0,
                "gain_min_db": 0.0,
                "gain_max_db": 49.6,
            }
        )
        == []
    )


def test_a_transient_surviving_a_flush_with_no_retune_is_the_flushs() -> None:
    """The control that separates a hop's two halves. If the output is still disturbed
    with the radio sitting exactly where it was, `activateStream` is what every hop
    waits for and discarding longer after each retune cannot be the fix."""
    findings = radio._flush_findings(
        {"measured": True, "settle_ms": 60.8}, {"measured": True, "settle_ms": 58.0}
    )

    assert findings and "the transient is the FLUSH" in findings[0]


def test_a_flush_that_settles_at_once_leaves_the_transient_with_the_tuner() -> None:
    """The other answer, and the one that keeps the settle a retune cost."""
    assert (
        radio._flush_findings(
            {"measured": True, "settle_ms": 60.8}, {"measured": True, "settle_ms": 0.4}
        )
        == []
    )


def test_the_discard_covers_the_measured_worst_case_not_the_median() -> None:
    """MEASURED on the box: a retune disturbs the output for 59.7 ms typically and
    131.2 ms at WORST, so a discard sized to the median leaves every slower hop reading
    the frequency it came from."""
    driver = _FakeDriver()

    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        dropped = rig.barrier()

    assert dropped == int(RATE * radio.SETTLE_S)
    assert radio.SETTLE_S * 1000 > 131.2


def test_a_transient_that_is_wrong_but_steady_defeats_a_level_based_stopping_rule() -> (
    None
):
    """Why the discard is FIXED rather than adaptive, kept as a test because the
    adaptive version was written, shipped and reverted in one day.

    A discard cannot know the new frequency's settled level — that is the thing it is
    waiting for — so the only rule available to it is "has the level stopped moving".
    A transient that steps to a wrong level and HOLDS satisfies that immediately. It is
    not a tuning problem: without a reference there is nothing to have arrived at."""
    driver = _FakeDriver(settling_step=True)

    with radio.Radio.open(driver=driver, rate_hz=RATE, center_hz=CENTER) as rig:
        rig.retune(center_hz=CENTER + RATE)
        levels = radio._window_levels(
            rig.read(radio.SETTLE_WINDOW * 4).samples, radio.SETTLE_WINDOW
        )

    # Flat to well inside any "stopped moving" tolerance, and still not the real level.
    assert float(np.max(levels) - np.min(levels)) < 0.5


def test_a_discard_that_leaves_the_transient_behind_is_a_finding() -> None:
    """The check on the FIX, not on the radio. The discard's stopping rule is weaker
    than the one that measured the settle: it asks whether the level has stopped MOVING,
    because the new frequency's settled level is exactly what it cannot know. A slow
    transient satisfies that while still failing "has it arrived" — which would put the
    stale samples back, the very bug the discard exists to remove."""
    findings = radio._barrier_findings(
        {"measured": True, "worst_ms": 41.0, "window_us": 213.0, "hold_ms": 10.0}
    )

    assert findings and "STOPS EARLY" in findings[0]


def test_a_discard_that_leaves_nothing_behind_is_not_a_finding() -> None:
    """Below the method's own resolution there is nothing left to report."""
    assert (
        radio._barrier_findings(
            {"measured": True, "worst_ms": 0.0, "window_us": 213.0, "hold_ms": 10.0}
        )
        == []
    )
