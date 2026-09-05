"""The radio behind a small protocol: the device half of the I/Q spectrum engine.

`iq.py` is samples in, one row out, and deliberately holds no radio. This is the other
half — the only file in this sidecar that ever opens a device — and it is a separate
module for a reason that is not tidiness: **`import SoapySDR` must not happen when
`listen.py` is imported**. `supervisor/tests/` loads `deploy/sdr/*.py` BY PATH with this
directory on `sys.path`, and SoapySDR is an apt package present in the image and in no
venv, so a module-scope import here would break collection of the whole supervisor
suite the moment anything imports this file (docs/plans/SDR_IQ_SPECTRUM_PLAN.md §6.1).
A module boundary defers nothing on its own; the import is inside `_Soapy.__init__`,
which nothing constructs until a real device is wanted.

That is also what makes the rules below testable. Everything SoapySDR-shaped is reached
through `Driver` and `Device` — two Protocols naming the dozen calls this file makes —
so a fake device proves the ORDERING, which is where every one of these bugs lives:

**The retune barrier is two steps, and the asymmetry is the whole point.** Only
`setSampleRate` sets librtlsdr's `resetBuffer`; `setFrequency` and
`writeSetting("direct_samp")` do not. So after a plain retune `readStream` keeps handing
back up to `numBuffers * bufflen` samples of the OLD band — 0.8 s at 2.4 MS/s, 7.9 s at
250 kS/s — which `iq.py` would stamp with the NEW centre. A frame labelled 7.20-7.45 MHz
carrying 14.15-14.35 MHz is the same class of lie `duty` and `uncovered` exist to
prevent, and it is worse across a `direct_samp` change, where the ADC branch itself
moved. Calling `activateStream` again on a RUNNING stream is a pure FIFO flush with no
rebuild (it sets `resetBuffer`, zeroes `bufferedElems`, and starts the async thread only
`if (not joinable())`), so: re-activate to empty the software ring, THEN discard
`rate * settle` for the hardware pipeline `resetBuffer` does not reach (§3).

**`direct_samp` before `setFrequency`**, because `rtlsdr_set_direct_sampling` ends by
re-applying the PREVIOUS mode's centre (§6.15). The frequency write is last in
`retune()` for that reason and no other.

**The NAMED `setFrequency` overload.** The 3-argument form distributes the value over
`listFrequencies()` — `{"RF", "CORR"}` on SoapyRTLSDR — so it zeroes any ppm correction
and pays an extra I2C write per retune (§6.15).

**One small `bufflen`, validated in code.** `bufflen` is a `getStreamArgsInfo` STREAM
ARG, consumed once inside `setupStream` and immutable for the life of the stream — not
a `writeSetting` key. Its default of 262,144 bytes is 131,072 complex samples per USB
callback, and a frame cannot arrive faster than one callback: 1.9 fps at 250 kS/s, which
is slower than the `rtl_power` this engine replaces. `BUFFLEN_BYTES` is one small value
for every rate, so `setSampleRate` stays live (a zoom needs no stream rebuild) and a
high rate simply means more callbacks per frame (§3).

**The reason it is asserted rather than trusted:** librtlsdr does
`if (buf_len > 0 && buf_len % 512 == 0) dev->xfer_buf_len = buf_len; else ... =
DEFAULT_BUF_LENGTH;` — a mis-computed value does not error, it silently restores the
exact ceiling this engine exists to remove. `getStreamMTU` reports the REQUESTED value,
so it is not a check either; the only honest verification is measuring the callback
period on a real dongle, which is what `probe()` does.

**Teardown order is load-bearing**: `deactivateStream` -> `closeStream` -> `unmake`.
`~SoapyRTLSDR()` is a bare `rtlsdr_close` with no stream teardown, and
`deactivateStream` is what joins the async reader thread. Which leads to the last rule:

**An open device handle is discoverable, because `/reset` has to see it.** A leaked
child process is reaped on the next sweep (`listen.reap_survivors`, commit `1a64ad0`); a
leaked device handle has no reap path at all and lives for the container's lifetime. And
`USBDEVFS_RESET` fired from the same process that still holds the usbfs fd is the
dangerous case: the device re-enumerates at a new node, the orphaned libusb handle goes
ENODEV and is never closed, and nothing here would learn. `holders()` is what
`server.py`'s `/reset` asks before it fires the ioctl (§3).
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

import iq

#: The USB transfer size handed to `setupStream`, in BYTES. 97 * 512.
#:
#: ONE value for every rate, not one per section. `bufflen` is the USB transfer size,
#: not the frame size, and nothing requires it to track the rate: this is 99 ms per
#: callback at the lowest rate the band table uses (256 kS/s) and 10.3 ms at the highest
#: (2.4 MS/s) — ~100 completions a second across 15 in-flight transfers, which libusb
#: does not notice. The consequence that matters is that a rate change is
#: `setSampleRate` ALONE, with no `setupStream` argument to change and therefore no
#: stream rebuild, so zooming stays live (§3).
BUFFLEN_BYTES = 49_664

#: What librtlsdr uses when it rejects a `bufflen`, and it rejects SILENTLY: 16*32*512.
#: Here so the probe can say which of the two it measured rather than just "slow".
DEFAULT_BUFFLEN_BYTES = 262_144

#: `xfer_buf_len` is taken only if it is a positive multiple of this.
BUFFLEN_QUANTUM = 512

#: Bytes per complex sample ON THE WIRE — u8 I plus u8 Q. The stream format is CF32 and
#: SoapyRTLSDR converts inside its own C++ loop, so this is about the USB buffer, which
#: is what `bufflen` sizes.
WIRE_BYTES_PER_SAMPLE = 2

#: How much to discard after the FIFO flush, for the hardware the flush cannot reach:
#: PLL relock plus whatever the RTL2832U's own decimation pipeline is carrying. The
#: third draft of the plan specified the barrier as this alone, which cannot work — the
#: software backlog's quantum is a whole USB buffer, orders of magnitude bigger. It is
#: the SECOND step, and it is small because the first one did the heavy lifting.
#: MEASURED on the box, 2026-09-05, and no longer a guess: a retune disturbs the output
#: for 59.7 ms typically and 131.2 ms at WORST, while a flush with no frequency change
#: disturbs it for 0.0. So this covers the worst case with margin, and it is fixed rather
#: than adaptive on purpose.
#:
#: An adaptive discard was tried and REVERTED the same day. It stopped when the level
#: stopped moving, because the new frequency's settled level is exactly what a discard
#: cannot know — and that rule cannot tell "settled" from "steadily WRONG". A transient
#: that steps to a wrong level and holds satisfies it immediately, which is not a tuning
#: problem but the rule being unsound: without a reference there is nothing to have
#: arrived AT. The fake's step-shaped transient says so, and it costs a hop's worth of
#: correctness to find out on air.
#:
#: The price is real and belongs here rather than in a commit message: eleven hops on
#: the FM dial pay this once each, so a 20 MHz span redraws about twice a second. Making
#: that faster means FEWER HOPS, not a shorter discard.
SETTLE_S = 0.15

#: `readStream`'s per-call timeout. Ten buffers at the slowest rate: long enough that an
#: ordinary scheduling hiccup is not an event, short enough that a dead stream is.
READ_TIMEOUT_US = 1_000_000

#: `SoapyRTLSDR` exposes one RX channel and this file has no use for a second.
CHANNEL = 0

#: The `writeSetting` key for the ADC branch, and the value this board wants. The NESDR
#: SMArt v5 wires the Q branch; `rtl_power -D` hardcodes mode 1 (I), which is the whole
#: reason shortwave has never been sweepable here (§1).
DIRECT_SAMP = "direct_samp"
DIRECT_Q_BRANCH = "2"
DIRECT_OFF = "0"

#: The device key an unnamed handle takes — the same value as `listen.ANY_DEVICE`, and
#: NOT imported from there: `listen` imports this module at F6, and a cycle between the
#: two would be paid for at import time by every caller. `test_sdr_radio.py` asserts the
#: two are equal, so the copy cannot drift into a second answer about what blocks what.
ANY_DEVICE = ""


class RadioError(RuntimeError):
    """The device would not open, would not configure, or stopped delivering."""


class RadioBusy(RuntimeError):
    """This process already holds a device handle that covers the radio asked for."""


class StreamResult(Protocol):
    """What `readStream` returns: a count, or a negative SoapySDR error code."""

    ret: int
    flags: int


class Device(Protocol):
    """The `SoapySDR.Device` calls this file makes, and no others.

    camelCase because that is SoapySDR's C++ API surface coming through SWIG unchanged;
    renaming it here would mean a shim whose only job is to be pretty."""

    def setSampleRate(self, direction: int, channel: int, rate: float) -> None: ...

    def getSampleRate(self, direction: int, channel: int) -> float: ...

    def setFrequency(
        self, direction: int, channel: int, name: str, hz: float
    ) -> None: ...

    def writeSetting(self, key: str, value: str) -> None: ...

    def getGainMode(self, direction: int, channel: int) -> bool: ...

    def setGainMode(self, direction: int, channel: int, automatic: bool) -> None: ...

    def getGain(self, direction: int, channel: int) -> float: ...

    def setGain(self, direction: int, channel: int, value: float) -> None: ...

    def getGainRange(self, direction: int, channel: int) -> Any: ...

    def readSetting(self, key: str) -> str: ...

    def getHardwareInfo(self) -> dict[str, str]: ...

    def setupStream(
        self, direction: int, fmt: str, channels: list[int], args: dict[str, str]
    ) -> Any: ...

    def activateStream(self, stream: Any) -> int: ...

    def deactivateStream(self, stream: Any) -> int: ...

    def closeStream(self, stream: Any) -> None: ...

    def readStream(
        self,
        stream: Any,
        buffs: list[Any],
        numElems: int,
        flags: int,
        timeoutUs: int,
    ) -> StreamResult: ...


class Driver(Protocol):
    """Enumeration, construction and destruction, plus the constants that come with
    them. Separate from `Device` because `unmake` is a static function on the SoapySDR
    class rather than a method on the handle, and because a fake needs somewhere to put
    the error codes."""

    RX: int
    CF32: str
    OVERFLOW: int
    TIMEOUT: int

    def enumerate(self, args: dict[str, str]) -> list[dict[str, str]]: ...

    def make(self, args: dict[str, str]) -> Device: ...

    def unmake(self, device: Device) -> None: ...

    def version(self) -> str: ...


class _Soapy:
    """The real SoapySDR behind `Driver`, with the import deferred to construction.

    Nothing at module scope touches SoapySDR, so importing `radio` costs nothing and
    works everywhere; constructing this is what requires the image."""

    def __init__(self) -> None:
        try:
            import SoapySDR  # noqa: PLC0415 - deferred on purpose; see the docstring
        except ImportError as missing:  # pragma: no cover - image-only path
            raise RadioError(
                "SoapySDR is not installed here — the spectrum engine needs "
                "`python3-soapysdr` and `soapysdr-module-rtlsdr` from the image"
            ) from missing
        self._sdr = SoapySDR
        self.RX: int = SoapySDR.SOAPY_SDR_RX
        self.CF32: str = SoapySDR.SOAPY_SDR_CF32
        self.OVERFLOW: int = SoapySDR.SOAPY_SDR_OVERFLOW
        self.TIMEOUT: int = SoapySDR.SOAPY_SDR_TIMEOUT

    def enumerate(self, args: dict[str, str]) -> list[dict[str, str]]:
        return [dict(found) for found in self._sdr.Device.enumerate(args)]

    def make(self, args: dict[str, str]) -> Device:
        """Open by ARGS STRING, never by dict. MEASURED on the box, 2026-09-05.

        With two dongles attached and the rtlsdr module registered:

            Device({"driver": "rtlsdr", "serial": "09022796"})  make() no match
            Device("driver=rtlsdr,serial=09022796")             OPENED

        A dict carrying a `driver` key is what fails, and it fails only in `make` —
        `enumerate` handed the SAME dict returns both devices, and a dict with no
        `driver` key opens. So the binding's dict typemap produces a value that
        compares equal inside one code path and not the other, and the string form,
        which SoapySDR parses in C++ for itself, sidesteps the question entirely.

        No radio serial or driver name contains `,` or `=`, so the join is unambiguous
        for everything this file passes."""
        joined = ",".join(f"{k}={v}" for k, v in args.items())
        return self._sdr.Device(joined)  # type: ignore[no-any-return]

    def open_diagnosis(self, args: dict[str, str]) -> dict[str, Any]:
        """Say WHY `make` found no match, by asking the same question three ways.

        MEASURED 2026-09-05 on the box: enumeration returns two dongles, driver
        `rtlsdr`, both serials intact — and `make({"driver": "rtlsdr", "serial":
        "09022796"})`, a filter matching one of them exactly, answers `make() no
        match`. It fails the same way with no serial at all, so the filter is not it.

        The first draft of this blamed the SWIG call shape, and that was falsified
        before it shipped. Against SoapySDR 0.8.1 with the rtlsdr module present and
        NO hardware attached, all four shapes reach the driver's own factory and raise
        `No RTL-SDR devices found!` — the DRIVER's sentence, not SoapySDR's. So
        `make() no match` is raised before any factory runs, and a call shape cannot be
        what separates them.

        What can: which find functions this process has registered, and what
        `enumerate` answers for the exact args `make` rejects. Both are read here. The
        shapes are kept because they cost nothing and pin that result to the box's own
        0.8.0 rather than to a 0.8.1 measured elsewhere.

        A diagnostic, not a fallback: nothing in the live path calls it, and the engine
        keeps one documented way to open a radio."""
        serial = args.get("serial")
        ladder: list[tuple[str, dict[str, str]]] = [
            ("no filter at all", {}),
            # The control, and the sharpest reading here. MEASURED against 0.8.1: a
            # driver NAME THAT DOES NOT EXIST is what raises `make() no match`, while a
            # registered driver with no hardware raises the driver's own sentence. So if
            # this answers exactly what `driver only` answers, `rtlsdr` is as
            # unregistered in this process as a name nobody ever wrote.
            (CONTROL_FILTER, {"driver": "definitelynotadriver"}),
            ("driver only", {"driver": "rtlsdr"}),
        ]
        if serial:
            ladder.append(("serial only", {"serial": serial}))
            ladder.append(("driver + serial", {"driver": "rtlsdr", "serial": serial}))
        # Handed back exactly what enumeration produced, keys and all — the one filter
        # that cannot be wrong about what this driver calls its own devices.
        for row in self._rows():
            if not serial or row.get("serial") == serial:
                ladder.append(("the enumeration row itself", row))
                break
        return {
            "environment": self._environment(),
            "filters": [
                {"filter": label} | self._filter_result(filt) for label, filt in ladder
            ],
            "shapes": self._shapes(args),
        }

    def _rows(self) -> list[dict[str, str]]:
        try:
            return self.enumerate({"driver": "rtlsdr"})
        except Exception:  # noqa: BLE001 - a diagnosis must not die diagnosing
            return []

    def _environment(self) -> dict[str, Any]:
        """What SoapySDR this process actually is, and what it has loaded.

        `modules` is the decisive one: a registry with no rtlsdr entry means enumeration
        is answering from somewhere `make` never looks, which is a packaging fault
        rather than a code one."""
        sdr = self._sdr
        env: dict[str, Any] = {}
        readings: list[tuple[str, Any]] = [
            ("api", sdr.getAPIVersion),
            ("abi", sdr.getABIVersion),
            ("lib", sdr.getLibVersion),
            ("root", sdr.getRootPath),
            ("modules", lambda: [str(m) for m in sdr.listModules()]),
            ("search_paths", lambda: [str(p) for p in sdr.listSearchPaths()]),
            ("module_errors", self._module_errors),
            ("binding", lambda: str(getattr(sdr, "__file__", "?"))),
            ("linked", _linked_soapy),
            ("util_find", lambda: _soapy_util("--find=driver=rtlsdr")),
        ]
        for key, call in readings:
            try:
                env[key] = call()
            except Exception as failed:  # noqa: BLE001 - name it, do not raise it
                env[key] = f"{type(failed).__name__}: {failed}"[:160]
        return env

    def _module_errors(self) -> dict[str, str]:
        """Modules that loaded but did NOT register, keyed by path.

        `getLoaderResult` is empty for a clean load and carries the reason otherwise —
        an ABI mismatch, a missing symbol, a driver name already taken. A module can be
        on the search path, be listed, and still have registered nothing, which is the
        one state where `enumerate` and `make` could honestly disagree."""
        sdr = self._sdr
        broken: dict[str, str] = {}
        for module in sdr.listModules():
            result = sdr.getLoaderResult(module)
            # MEASURED and corrected: this is NOT an error string. It maps each driver
            # the module registered to that driver's error, and a registered driver has
            # an EMPTY one — the box returned `{"rtlsdr": ""}` for a module that had
            # just registered rtlsdr perfectly well, and reading the keys called a
            # success a failure. Only a non-empty value is a failure.
            if hasattr(result, "keys"):
                why = "; ".join(f"{k}: {v}" for k, v in dict(result).items() if v)
            else:
                why = str(result)
            if why:
                broken[str(module)] = why[:200]
        return broken

    def _filter_result(self, filt: dict[str, str]) -> dict[str, str]:
        """`enumerate` and `make` on the SAME args, side by side.

        They are supposed to agree. Where they do not, the disagreement is the finding."""
        try:
            listed = str(len(self.enumerate(filt)))
        except Exception as failed:  # noqa: BLE001 - naming the failure IS the job
            listed = f"raised {type(failed).__name__}: {failed}"[:120]
        try:
            device = self._sdr.Device(filt)
        except Exception as failed:  # noqa: BLE001 - naming the failure IS the job
            return {"enumerate": listed, "make": f"{failed}"[:160]}
        with contextlib.suppress(Exception):
            self.unmake(device)
        return {"enumerate": listed, "make": "opened"}

    def _shapes(self, args: dict[str, str]) -> list[dict[str, str]]:
        """Every plausible way to ask the binding, in case 0.8.0 differs from 0.8.1."""
        sdr = self._sdr
        joined = ",".join(f"{k}={v}" for k, v in args.items())
        shapes: list[tuple[str, Any]] = [
            ("Device(dict)", lambda: sdr.Device(args)),
            ("Device.make(dict)", lambda: sdr.Device.make(args)),
            (f"Device({joined!r})", lambda: sdr.Device(joined)),
            ("Device(KwargsFromString)", lambda: sdr.Device(sdr.KwargsFromString(joined))),
        ]
        tried: list[dict[str, str]] = []
        for name, call in shapes:
            try:
                device = call()
            except Exception as failed:  # noqa: BLE001 - naming the failure IS the job
                tried.append({"shape": name, "opened": "no", "error": f"{failed}"[:160]})
                continue
            tried.append({"shape": name, "opened": "yes"})
            with contextlib.suppress(Exception):
                self.unmake(device)
            break
        return tried

    def unmake(self, device: Device) -> None:
        self._sdr.Device.unmake(device)

    def version(self) -> str:
        return f"api {self._sdr.getAPIVersion()} abi {self._sdr.getABIVersion()}"


def _answered(step: str, run: Any) -> dict[str, Any]:
    """Run one of the probe's checks and answer for it, whatever it does.

    MEASURED 2026-09-05: the first hardware run raised a call-shape error inside one
    check, and because the guards around the others were narrow — `(RadioError,
    ValueError)`, which an `AttributeError` from a wrong SWIG signature sails past — the
    whole probe came back as a single 502 and six questions went unasked. A probe exists
    to answer all seven in ONE run, on hardware that is not always at hand, so the guard
    has to be as wide as the mistakes it is looking for."""
    _log(f"probe: {step}")
    try:
        got = run()
    except Exception as failed:  # noqa: BLE001 - the whole point; see above
        _log(f"probe: {step} FAILED {type(failed).__name__}: {failed}")
        return {
            "works": False,
            "error": f"{type(failed).__name__}: {failed}",
            "where": step,
        }
    return got if isinstance(got, dict) else {"works": True, "value": got}


def _log(message: str) -> None:
    print(f"[radio] {message}", flush=True)  # noqa: T201 - the container log is the trail


def _version_or_error(drv: Driver) -> str:
    """The driver's version, or why it could not be asked.

    Reported rather than raised because it is the FIRST call the probe makes: a binding
    mismatch here would otherwise take down the whole verdict before a single claim was
    tested, which is the failure the wrapper in `probe` exists to stop."""
    try:
        return drv.version()
    except Exception as failed:  # noqa: BLE001 - a version string is not worth a 502
        return f"unavailable: {type(failed).__name__}: {failed}"


def validate_bufflen(bufflen_bytes: int) -> int:
    """Refuse a `bufflen` librtlsdr would silently replace. Returns it unchanged.

    THE point of this function is that the failure it prevents is invisible: a value
    that is not a positive multiple of 512 is not an error, it is a quiet fallback to
    262,144 bytes and the 1.9 fps ceiling this whole engine exists to remove. Raising
    here converts a wrong picture nobody can see into a refusal at startup."""
    if bufflen_bytes <= 0 or bufflen_bytes % BUFFLEN_QUANTUM:
        raise ValueError(
            f"bufflen must be a positive multiple of {BUFFLEN_QUANTUM} bytes, "
            f"got {bufflen_bytes} — librtlsdr would silently use "
            f"{DEFAULT_BUFFLEN_BYTES} instead"
        )
    return bufflen_bytes


def samples_per_buffer(bufflen_bytes: int = BUFFLEN_BYTES) -> int:
    """Complex samples in one USB transfer — the quantum a frame is built out of."""
    return bufflen_bytes // WIRE_BYTES_PER_SAMPLE


# Every radio THIS PROCESS holds a device handle on. Keyed exactly as `listen.Tuner` is
# (serial, or `ANY_DEVICE` for an unnamed handle) so `listen.blocking_key` can be asked
# about both registries with one rule rather than two that can disagree.
_open: dict[str, "Radio"] = {}
_open_lock = threading.Lock()


def holders() -> dict[str, str]:
    """Every radio this process has a device handle on -> what it is doing.

    The lease knows about child processes and TTL reservations; it cannot see an
    in-process handle, and the leak case is exactly the dangerous one — the lease
    believes the radio is free (that is what leaked means), so `/reset` fires
    `USBDEVFS_RESET` from the process still holding the usbfs fd with interface 0
    claimed. Feed this to `listen.blocking_key` alongside the lease's own view."""
    with _open_lock:
        return {key: radio.doing for key, radio in _open.items()}


def _claim(key: str, radio: "Radio") -> None:
    with _open_lock:
        held = _open.get(key)
        if held is not None:
            raise RadioBusy(f"this process already holds a device handle ({held.doing})")
        _open[key] = radio


def _release(key: str, radio: "Radio") -> None:
    with _open_lock:
        if _open.get(key) is radio:
            _open.pop(key, None)


@dataclass(frozen=True, slots=True, eq=False)
class Reading:
    """One frame's samples, and what the stream did while they were being collected.

    `overflows` is a COUNT rather than a log line, because it is the only honest
    statement about a frame's continuity: `readStream` returning `SOAPY_SDR_OVERFLOW`
    means the RTL2832U's FIFO filled and the driver threw a buffer away, so the samples
    either side of it are not adjacent in time. `rtl_sdr` had no such signal at all —
    when the callback blocks, libusb simply stops resubmitting and the drop is silent
    (§3) — so this number is new information, and it rides on the frame rather than
    going to a log nobody reads.

    `eq=False` for `iq.Spectrum`'s reason: equality on a dataclass holding an ndarray
    returns an array, so `a == b` would raise rather than answer."""

    samples: np.ndarray
    at: float
    reads: int
    overflows: int
    timeouts: int

    @property
    def torn(self) -> bool:
        """Whether anything was dropped inside this frame."""
        return self.overflows > 0


class Radio:
    """One open device and one activated stream, with the ordering rules enforced.

    Constructed through `open()`, which registers the handle BEFORE it makes the device:
    a half-open handle is exactly the thing `/reset` must not fire under, so it has to be
    visible from the moment the attempt starts rather than from the moment it
    succeeds."""

    def __init__(
        self,
        driver: Driver,
        *,
        serial: str | None,
        bufflen_bytes: int = BUFFLEN_BYTES,
        doing: str = "reading I/Q",
    ) -> None:
        self._driver = driver
        self.serial = serial
        self.key = serial or ANY_DEVICE
        self.bufflen_bytes = validate_bufflen(bufflen_bytes)
        self.samples_per_buffer = samples_per_buffer(self.bufflen_bytes)
        self._doing = doing
        self._device: Device | None = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self._rate_hz = 0
        self._achieved_rate_hz = 0.0
        self._center_hz = 0
        # None rather than False, so the FIRST configure always writes the branch
        # explicitly instead of inheriting whatever the last holder left it in.
        self._direct: bool | None = None
        self._rate_warning: str | None = None

    @classmethod
    def open(
        cls,
        *,
        rate_hz: int,
        center_hz: int,
        serial: str | None = None,
        direct: bool = False,
        bufflen_bytes: int = BUFFLEN_BYTES,
        driver: Driver | None = None,
        doing: str = "reading I/Q",
    ) -> "Radio":
        """Open one radio, configure it, and start its stream. Never blocks."""
        radio = cls(
            driver if driver is not None else _Soapy(),
            serial=serial,
            bufflen_bytes=bufflen_bytes,
            doing=doing,
        )
        _claim(radio.key, radio)
        try:
            radio._start(rate_hz=rate_hz, center_hz=center_hz, direct=direct)
        except BaseException:
            # Including KeyboardInterrupt and SystemExit: a device made and then
            # abandoned by a signal is the leak with no reap path. A close that fails
            # is suppressed HERE only — it keeps its registry entry, so the handle is
            # still discoverable — because the caller needs to see why the open failed,
            # not why the cleanup after it did.
            with contextlib.suppress(RadioError):
                radio.close()
            raise
        return radio

    @property
    def doing(self) -> str:
        return self._doing

    @property
    def alive(self) -> bool:
        return self._device is not None and self._stream is not None

    @property
    def rate_hz(self) -> int:
        """The REQUESTED rate — what `start_hz` and `bin_hz` must be derived from.

        Not the achieved one, and this is not a rounding preference: librtlsdr
        quantises to the 28.8 MHz divider, and a +-1 Hz flap in a derived `start_hz`
        re-blanks the PWA's waterfall and re-freezes its colour scale on every frame
        (§6.8). The band table picks rates that come back unchanged; `achieved_rate_hz`
        is what proves it did, and is for reporting, not for labelling frames."""
        return self._rate_hz

    @property
    def achieved_rate_hz(self) -> float:
        """What `getSampleRate` says the hardware really runs at."""
        return self._achieved_rate_hz

    @property
    def rate_warning(self) -> str | None:
        """Set when the achieved rate differs from the requested one."""
        return self._rate_warning

    @property
    def center_hz(self) -> int:
        return self._center_hz

    @property
    def direct(self) -> bool:
        return bool(self._direct)

    @property
    def stream_token(self) -> int:
        """Identity of the stream handle now. `setupStream` is called exactly once, in
        `_start`, so comparing this across a retune is what proves nothing was rebuilt —
        which is the claim the whole engine choice rests on (§3)."""
        return id(self._stream)

    def hardware_info(self) -> dict[str, str]:
        """What the driver says about the device it opened."""
        return dict(self._require_device().getHardwareInfo())

    def read_setting(self, key: str) -> str:
        """What a setting reads back as NOW. `writeSetting` returns nothing, so a value
        the driver declined is indistinguishable from one it took until this is asked."""
        return str(self._require_device().readSetting(key))

    def __enter__(self) -> "Radio":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _require_device(self) -> Device:
        """The device alone — everything up to `setupStream` has no stream yet."""
        if self._device is None:
            raise RadioError("this radio has been closed")
        return self._device

    def _require(self) -> tuple[Device, Any]:
        device, stream = self._device, self._stream
        if device is None or stream is None:
            raise RadioError("this radio has no active stream")
        return device, stream

    def _start(self, *, rate_hz: int, center_hz: int, direct: bool) -> None:
        args = {"driver": "rtlsdr"}
        if self.serial:
            # `serial=` is a key librtlsdr's own `verbose_device_search` has no form
            # for; SoapySDR's enumeration does, which is how a two-dongle box addresses
            # one radio without depending on enumeration order.
            args["serial"] = self.serial
        try:
            self._device = self._driver.make(args)
        except Exception as failed:
            # The ARGS go in the sentence. MEASURED 2026-09-05: the box answered
            # `SoapySDR::Device::make() no match` for a filter as plain as
            # `{"driver": "rtlsdr"}` while enumeration was finding devices, and without
            # the args in the message there is no way to tell a wrong filter from a
            # driver that is not loaded — which are opposite fixes.
            raise RadioError(f"could not open the radio with {args} ({failed})") from failed
        device = self._device
        # Configured BEFORE `setupStream`, and not only by convention: `bufflen` is a
        # stream argument consumed once inside it, so the stream is built against a
        # radio that is already on frequency and at rate.
        self._apply(rate_hz=rate_hz, center_hz=center_hz, direct=direct)
        try:
            self._stream = device.setupStream(
                self._driver.RX,
                self._driver.CF32,
                [CHANNEL],
                # Validated in `validate_bufflen`, stringified because stream args are
                # a string map. `buffers` is left at the driver's 15: the count is what
                # bounds the backlog a retune has to flush, and 15 small ones is 1.5
                # buffers' worth of latency rather than 15 large ones.
                {"bufflen": str(self.bufflen_bytes)},
            )
            device.activateStream(self._stream)
        except Exception as failed:
            raise RadioError(f"could not start the stream ({failed})") from failed
        # The first barrier: the stream has just been activated, so the software ring is
        # already empty and only the hardware settle is really needed — but it goes
        # through the same code path as every later retune, because a first frame that
        # is subtly different from the rest is a bug that only shows up once.
        self.barrier()

    def retune(
        self,
        *,
        center_hz: int | None = None,
        rate_hz: int | None = None,
        direct: bool | None = None,
        settle_s: float | None = None,
    ) -> int:
        """Move the radio and rebuild the truth about what is in the FIFO.

        Returns the samples the barrier discarded. Every configuration change goes
        through here, including the first, because the ORDER is the content:

        1. **rate**, which is the only one of the three that flushes anything by itself.
        2. **`direct_samp`**, before the frequency, because
           `rtlsdr_set_direct_sampling` re-applies the previous mode's centre as it
           leaves (§6.15). A branch change therefore re-writes the frequency even when
           the caller did not ask for a new one.
        3. **frequency**, through the NAMED overload — the 3-argument form distributes
           over `listFrequencies()` = {"RF", "CORR"} and zeroes any ppm correction.
        4. **the barrier**, because none of the above emptied the ring."""
        self._apply(center_hz=center_hz, rate_hz=rate_hz, direct=direct)
        return self.barrier(settle_s)

    def gain_state(self) -> dict[str, Any]:
        """What the gain is doing right now — its own loop, or a number we chose.

        Suppressed rather than required, because a driver that cannot answer is a
        reading this probe does without, not a reason to fail the whole run."""
        device = self._require_device()
        out: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            out["automatic"] = bool(device.getGainMode(self._driver.RX, CHANNEL))
        with contextlib.suppress(Exception):
            out["gain_db"] = round(float(device.getGain(self._driver.RX, CHANNEL)), 2)
        with contextlib.suppress(Exception):
            span = device.getGainRange(self._driver.RX, CHANNEL)
            out["gain_min_db"] = round(float(span.minimum()), 2)
            out["gain_max_db"] = round(float(span.maximum()), 2)
        return out

    def set_gain(self, db: float | None) -> None:
        """Nail the gain down, or hand it back to the radio's own loop with None.

        A spectrum instrument wants the first: a waterfall whose gain moves has a dB
        scale that means nothing from row to row, and every hop seam becomes a gain
        step drawn as if the band had changed."""
        device = self._require_device()
        if db is None:
            device.setGainMode(self._driver.RX, CHANNEL, True)
            return
        device.setGainMode(self._driver.RX, CHANNEL, False)
        device.setGain(self._driver.RX, CHANNEL, float(db))

    def _apply(
        self,
        *,
        center_hz: int | None = None,
        rate_hz: int | None = None,
        direct: bool | None = None,
    ) -> None:
        """Steps 1-3 of `retune`, without the barrier — the only caller that wants them
        apart is `_start`, where there is no stream to flush yet."""
        device = self._require_device()
        if rate_hz is not None and int(rate_hz) != self._rate_hz:
            if rate_hz <= 0:
                raise ValueError("a sample rate must be positive")
            device.setSampleRate(self._driver.RX, CHANNEL, float(rate_hz))
            self._rate_hz = int(rate_hz)
            self._achieved_rate_hz = float(
                device.getSampleRate(self._driver.RX, CHANNEL)
            )
            self._rate_warning = None
            if self._achieved_rate_hz != float(self._rate_hz):
                self._rate_warning = (
                    f"asked for {self._rate_hz} S/s, the divider gives "
                    f"{self._achieved_rate_hz:.4f}"
                )
                _log(self._rate_warning)
        if direct is not None and bool(direct) != self._direct:
            device.writeSetting(
                DIRECT_SAMP, DIRECT_Q_BRANCH if direct else DIRECT_OFF
            )
            self._direct = bool(direct)
            # Not optional: the centre the driver just restored is the OLD mode's.
            center_hz = self._center_hz if center_hz is None else center_hz
        if center_hz is not None:
            device.setFrequency(self._driver.RX, CHANNEL, "RF", float(center_hz))
            self._center_hz = int(center_hz)

    def barrier(self, settle_s: float | None = None) -> int:
        """Discard everything captured before the radio moved. Returns samples dropped.

        Step one: `activateStream` on a RUNNING stream, which is a pure FIFO flush —
        `resetBuffer = true`, `bufferedElems = 0`, and the async thread is started only
        `if (not joinable())`, so nothing is rebuilt and no device call is made.

        Step two: read and throw away `rate * settle`, for the hardware pipeline
        `resetBuffer` does not reach. Neither step alone is the barrier: the ring holds
        up to `numBuffers * bufflen` samples, which is far more than any settle window,
        and the flush cannot reach samples that are still inside the RTL2832U."""
        device, stream = self._require()
        device.activateStream(stream)
        settle = SETTLE_S if settle_s is None else float(settle_s)
        drop = int(self._rate_hz * settle)
        if drop <= 0:
            return 0
        scratch = np.empty(min(drop, self.samples_per_buffer), dtype=np.complex64)
        dropped = 0
        deadline = time.monotonic() + self._patience(drop)
        while dropped < drop:
            got = self.read_into(scratch[: min(scratch.size, drop - dropped)])
            if got > 0:
                dropped += got
            elif got not in (0, self._driver.OVERFLOW, self._driver.TIMEOUT):
                raise RadioError(f"the stream failed during a retune (code {got})")
            if time.monotonic() > deadline:
                # The settle is a discard, not a measurement: giving up on it costs at
                # worst a slightly stale first frame, while raising would turn a slow
                # moment into a lost session.
                _log(f"retune settle timed out with {dropped}/{drop} discarded")
                break
        return dropped

    def read_into(self, view: np.ndarray) -> int:
        """One `readStream` call. Returns its raw `ret` — negative codes included.

        Public because a caller measuring the USB callback period needs exactly one
        call at a time, and because that measurement is the only honest check that
        `bufflen` took (`probe`)."""
        device, stream = self._require()
        result = device.readStream(
            stream, [view], int(view.size), 0, READ_TIMEOUT_US
        )
        return int(result.ret)

    def _patience(self, samples: int) -> float:
        """How long `samples` may take before the stream counts as dead."""
        rate = self._rate_hz or 1
        return samples / rate * 4.0 + 1.0

    def read(self, samples: int) -> Reading:
        """Collect `samples` complex samples, counting what the stream reported.

        A short read is not an error and not a special case: `readStream` returns at
        most what is left of the USB buffer it is currently draining, so a frame at
        2.4 MS/s is assembled from ~10 calls with `bufflen` at its intended size. That
        is the design — `bufflen` sizes the transfer, not the frame."""
        if samples <= 0:
            raise ValueError("a frame needs at least one sample")
        buf = np.empty(int(samples), dtype=np.complex64)
        filled = reads = overflows = timeouts = 0
        at = time.time()
        deadline = time.monotonic() + self._patience(samples)
        while filled < samples:
            got = self.read_into(buf[filled:])
            reads += 1
            if got > 0:
                filled += got
            elif got == self._driver.OVERFLOW:
                overflows += 1
            elif got in (0, self._driver.TIMEOUT):
                timeouts += 1
            else:
                raise RadioError(f"readStream failed (code {got})")
            if time.monotonic() > deadline:
                raise RadioError(
                    f"the radio delivered {filled} of {samples} samples before the "
                    f"stream stopped answering"
                )
        return Reading(
            samples=buf, at=at, reads=reads, overflows=overflows, timeouts=timeouts
        )

    def close(self) -> None:
        """Give the device back, in the one order that gives it back cleanly.

        `deactivateStream` -> `closeStream` -> `unmake`, because `~SoapyRTLSDR()` is a
        bare `rtlsdr_close` with no stream teardown and `deactivateStream` is what joins
        the async reader thread. Idempotent, and safe on a radio whose `make` never
        succeeded — `open` calls it on exactly that path.

        A teardown that FAILS keeps its registry entry rather than dropping it. That
        looks backwards until you ask what the entry is for: it is what stops `/reset`
        firing a port reset from the process still holding the usbfs fd, and a handle we
        could not close is precisely the handle that is still held. The refusal then
        names the recovery that does work, which is restarting this service."""
        with self._lock:
            device, stream = self._device, self._stream
            self._device, self._stream = None, None
        if device is None:
            _release(self.key, self)
            return
        try:
            if stream is not None:
                device.deactivateStream(stream)
                device.closeStream(stream)
            self._driver.unmake(device)
        except Exception as failed:
            self._doing = (
                f"stuck with an open device handle ({failed}) — restart the sdr service"
            )
            _log(self._doing)
            raise RadioError(f"the radio would not close cleanly ({failed})") from failed
        _release(self.key, self)


#: Below this the R820T2 tuner cannot reach and the ADC is fed directly instead, which
#: is what `direct_samp` selects. The same figure as `listen.MIN_HZ`, and copied for the
#: same reason `ANY_DEVICE` is — this module imports no sibling that will import it back.
DIRECT_MAX_HZ = 24_000_000

#: What the R820T2 reaches. The same figure as `listen.MAX_HZ`, copied for the reason
#: `DIRECT_MAX_HZ` is: this module imports no sibling that will import it back.
MAX_TUNE_HZ = 1_766_000_000

#: WWV on 10 MHz: a carrier that is either there or not, on a frequency that is a fact
#: rather than a guess, reached through the direct-sampling branch this engine's whole
#: shortwave claim rests on. 256 kS/s over 1024 bins is 250 Hz exactly.
PROBE_CENTER_HZ = 10_000_000
PROBE_RATE_HZ = 256_000
PROBE_BINS = 1_024
#: Frames to read before judging. More than one because a single frame taken shortly
#: after the direct-sampling branch is switched reported a dead radio that the streaming
#: engine, on the same dongle at the same frequency, found perfectly alive — see
#: `_capture`. Ten is a third of a second at 256 kS/s: long enough to outlast a settle,
#: short enough that the probe is still an aside.
PROBE_CAPTURE_FRAMES = 10
#: Segments to Welch-average for the probe's one reading. Eight is 32 ms at 256 kS/s —
#: enough to pull a carrier clear of the noise, short enough to be an aside.
PROBE_SEGMENTS = 8
#: The rates the live-rate check walks, one from each part of the band table's range.
PROBE_RATES = (256_000, 1_024_000, 2_400_000)
#: How many USB buffers to time. The median of a dozen is stable against one scheduling
#: hiccup and still costs about a second at the slowest rate.
PROBE_CALLBACK_READS = 12
#: How long to stop reading for, to make the RTL2832U's FIFO overrun on purpose. The
#: driver's ring is 15 buffers — 0.15 s at 2.4 MS/s with `BUFFLEN_BYTES` — so this is
#: several times over.
PROBE_BACKPRESSURE_S = 1.0


def _reading_verdict(spectrum: iq.Spectrum) -> dict[str, Any]:
    """One captured frame reduced to the numbers that mean anything on their own.

    A peak in dBFS is uninterpretable by itself — the ADC has ~7 effective bits and no
    gain stage below 24 MHz — so it is reported against the frame's own median, which
    is the same relative standard `sweep.steady` uses and the reason a +6 dB rule was
    calibratable at all.

    **The centre bin is excluded, and that is not a detail.** Every direct-conversion
    receiver puts a DC offset spike exactly at the tuned frequency, so a peak there is
    the receiver looking at itself. MEASURED 2026-09-05: at 5.0, 7.15 and 10.0 MHz
    under `direct_samp=2` the frame was a DC delta and NOTHING else — peak exactly on
    the tuned bin at exactly +3.0 dBFS, every other bin exactly zero — and a plain
    argmax reported that as a 203 dB signal, three times, at three frequencies, without
    a murmur. `dead` is what says so instead: a frame whose median has fallen to
    `iq.DB_FLOOR` has no noise floor, and a receiver with no noise floor is not
    receiving. The same probe at 99.3 MHz gives a floor of -44 dBFS and a station 29 dB
    over it, which is what a live frame looks like."""
    db = spectrum.db
    dc = spectrum.bins // 2
    without_dc = np.delete(db, dc)
    top = int(np.argmax(without_dc))
    # `delete` closed the gap, so anything at or past the removed bin shifted down one.
    top_bin = top if top < dc else top + 1
    floor = float(np.median(without_dc))
    return {
        "bins": spectrum.bins,
        "bin_hz": spectrum.bin_hz,
        "segments": spectrum.segments,
        "peak_hz": float(spectrum.start_hz + top_bin * spectrum.bin_hz),
        "peak_db": round(float(db[top_bin]), 1),
        "dc_db": round(float(db[dc]), 1),
        "floor_db": round(floor, 1),
        "above_floor_db": round(float(db[top_bin]) - floor, 1),
        "dead": floor <= iq.DB_FLOOR,
    }


def _capture(radio: Radio, bins: int) -> dict[str, Any]:
    """Read SEVERAL frames and judge the last — the way the engine reads, not a snapshot.

    MEASURED 2026-09-05, and the reason this is no longer one frame. On the same radio
    at the same frequency, rate and bin count, under `direct_samp=2` at 7.2125 MHz:

        this probe, one frame     a DC spike and a median at DB_FLOOR — `dead`
        the live spectrum engine  a -51.5 dBFS floor with a peak 6.8 dB over it

    Both cannot be true of the radio, so one of them was true of the READING. A single
    frame taken shortly after the branch is switched is the weaker measurement, and the
    engine — which streams — is what the owner actually sees. So the probe now reads
    like the engine and reports `settled`: whether the FIRST frame was dead while the
    last was not. That turns "is this radio deaf on HF" from an inference into a
    measurement, which is the whole job of this file."""
    spectrometer = iq.Spectrometer(bins, radio.rate_hz)
    verdict: dict[str, Any] = {}
    first_dead: bool | None = None
    overflows = reads = 0
    for _ in range(PROBE_CAPTURE_FRAMES):
        reading = radio.read(bins * PROBE_SEGMENTS)
        overflows += reading.overflows
        reads += reading.reads
        verdict = _reading_verdict(spectrometer.frame(reading.samples, radio.center_hz))
        if first_dead is None:
            first_dead = bool(verdict["dead"])
    verdict.update(
        center_hz=radio.center_hz,
        direct_sampling=radio.direct,
        overflows=overflows,
        reads=reads,
        frames=PROBE_CAPTURE_FRAMES,
        # True when the radio needed a moment: the first frame had nothing in it and a
        # later one did. Not a fault — a fact about how long after the branch switch a
        # reading means anything, and one nothing else in this system measures.
        settled=bool(first_dead) and not verdict["dead"],
    )
    return verdict


def _callback_period(radio: Radio) -> dict[str, Any]:
    """Measure the USB callback period — the ONLY honest check that `bufflen` took.

    librtlsdr replaces a bad `bufflen` silently and `getStreamMTU` reports the value
    that was ASKED FOR, so neither the call succeeding nor the MTU agreeing proves
    anything. Two things here do. Each `readStream` returns at most what is left of the
    buffer it is draining, so asking for a DEFAULT-sized buffer's worth and getting
    `BUFFLEN_BYTES`' worth back says how big the transfers really are; and consuming as
    fast as they arrive makes each read block until the next callback, which times
    them."""
    radio.barrier(0.0)  # flush only: this times arrivals, so a settle would mask them
    scratch = np.empty(samples_per_buffer(DEFAULT_BUFFLEN_BYTES), dtype=np.complex64)
    gaps: list[float] = []
    elems: list[int] = []
    for _ in range(PROBE_CALLBACK_READS):
        started = time.monotonic()
        got = radio.read_into(scratch)
        if got > 0:
            gaps.append(time.monotonic() - started)
            elems.append(got)
    if not gaps:
        return {"took": False, "detail": "the stream delivered nothing to time"}
    measured_ms = float(np.median(gaps)) * 1000.0
    wanted_ms = radio.samples_per_buffer / radio.rate_hz * 1000.0
    fallback_ms = samples_per_buffer(DEFAULT_BUFFLEN_BYTES) / radio.rate_hz * 1000.0
    return {
        # Halfway between the two candidates IN RATIO, so the verdict does not depend on
        # how far apart they happen to be at this rate.
        "took": measured_ms < (wanted_ms * fallback_ms) ** 0.5,
        "bufflen_bytes": radio.bufflen_bytes,
        "samples_per_buffer": radio.samples_per_buffer,
        "elems_per_read": int(np.median(elems)),
        "callback_ms": round(measured_ms, 2),
        "expected_ms": round(wanted_ms, 2),
        "fallback_ms": round(fallback_ms, 2),
        "reads": len(gaps),
    }


#: One block, read straight after a retune with the timed discard switched off, and
#: sliced afterwards. Reading it in one go rather than as many small reads is what makes
#: the timing exact: a sample's age is its index over the rate, with no scheduling
#: jitter between it and a clock.
#:
#: Five times the settle it checks, because a stopwatch shorter than the event cannot
#: time it. MEASURED, 2026-09-05: the first version of this spanned 80 ms and reported
#: "80.0 ms at worst" on the tuner path — which was the span, not the radio.
SETTLE_SPAN_S = 0.4
#: Samples per slice. 512 at 2.4 MS/s is 213 us — fine enough to place a settle to a
#: fraction of a millisecond, long enough that a slice's mean power is a number rather
#: than a coin toss.
SETTLE_WINDOW = 512
#: How many times to do it. The transient is a hardware event and varies; one reading
#: would be an anecdote, and the median of a handful is what a constant should be set
#: from.
SETTLE_TRIALS = 7
#: How far a slice may sit from the settled level and still count as settled, in
#: multiples of the settled level's OWN slice-to-slice deviation. Three sigma, so
#: ordinary noise does not read as a transient and a real one is not missed.
SETTLE_SIGMA = 3.0
#: Slices in the running median. Five is enough to make an isolated outlier
#: impossible and short enough that it cannot hide a transient worth discarding.
SETTLE_SMOOTH = 5
#: How long the level must STAY inside the tolerance before the radio counts as settled.
#: Ten milliseconds is longer than any relock and far shorter than the slow drift — a
#: station fading, a gain step — that this rule exists to stop reading as a transient.
SETTLE_HOLD_S = 0.01


def _window_levels(samples: np.ndarray, window: int) -> np.ndarray:
    """Mean power per slice, in dB. The cheapest thing that moves when a tuner relocks.

    Power rather than a spectrum on purpose: two noise bands can have indistinguishable
    SHAPES, so a settle measured by comparing spectra would be unmeasurable exactly
    where it is least interesting. What a relocking tuner does to its output level is
    visible whatever is on the air."""
    usable = (samples.size // window) * window
    if usable < window:
        return np.empty(0, dtype=np.float64)
    blocks = samples[:usable].reshape(-1, window)
    power = np.mean(np.abs(blocks.astype(np.complex128)) ** 2, axis=1)
    return 10.0 * np.log10(np.maximum(power, iq._POWER_FLOOR))


def _smooth(levels: np.ndarray, width: int) -> np.ndarray:
    """A running median, so one wild slice cannot be mistaken for a transient."""
    if levels.size < width or width < 2:
        return levels
    pad = width // 2
    padded = np.pad(levels, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.median(windows, axis=-1)[: levels.size]


#: What to fix the gain at when the radio cannot say what it is currently using. Middle
#: of the R820T2's range: high enough to see a band, low enough not to clip a strong one.
SETTLE_FIXED_GAIN_DB = 30.0


def _settle_fixed_gain(radio: Radio, other_hz: int) -> dict[str, Any]:
    """The same stopwatch with the gain nailed down, so the two readings can be compared.

    The question this answers is what the settle IS. What the stopwatch watches is a
    LEVEL, and an automatic gain loop moves level for exactly the sort of duration
    measured here — 61 ms median on the tuner path, where an R820T2's PLL locks in well
    under a millisecond. If the settle collapses with the gain fixed, it was never a
    relock and the fix is to fix the gain, which a spectrum instrument wants anyway."""
    before = radio.gain_state()
    radio.set_gain(float(before.get("gain_db") or SETTLE_FIXED_GAIN_DB))
    try:
        out = _settle_after_retune(radio, other_hz)
    finally:
        # Back to whatever it was, because a probe that leaves the radio configured
        # differently from how it found it makes the NEXT reading a lie.
        if before.get("automatic"):
            radio.set_gain(None)
    out["gain_db"] = radio.gain_state().get("gain_db")
    out["was_automatic"] = before.get("automatic")
    return out


def _settled_index(off: np.ndarray, hold: int) -> int | None:
    """First slice from which the level stays inside tolerance for `hold` slices.

    Deliberately not "one past the last slice outside it". That rule reports the whole
    block whenever anything late wanders out, and over a span long enough to contain the
    transient, something late always does — drift, a station fading — so the reading
    saturates at the span and stops being a measurement. A sustained quiet run is what
    settled MEANS, and it also cannot be fooled by a transient that crosses the line and
    comes back, which is why the last-off rule was reached for in the first place.

    None when no such run exists: the radio had not settled by the end of the block, and
    the honest answer is "at least this span", not the span itself."""
    if off.size < hold or hold < 1:
        return None
    # Zero `off` slices in [i, i + hold) is exactly a zero in the moving sum.
    quiet = np.convolve(off.astype(np.int64), np.ones(hold, dtype=np.int64), "valid")
    starts = np.nonzero(quiet == 0)[0]
    return int(starts[0]) if starts.size else None


def _settle_after(radio: Radio, disturb: "Callable[[], None]") -> dict[str, Any]:
    """How long the radio's output stays disturbed after `disturb`, against what we
    assume — a discard of zero and a stopwatch made of sample indices.

    `SETTLE_S` was chosen, never measured, and it is the whole cost of a hopping sweep:
    the samples in a hop are microseconds and the discard is milliseconds, so this one
    number decides whether a wide band redraws twice a second or twenty times.

    `disturb` is a parameter rather than a fixed retune because a hop does TWO things —
    it retunes AND it flushes — and a measurement that always does both cannot say which
    one owns the transient. It must leave the radio back at home with no timed discard,
    so the block that follows starts at the instant the disturbance ends."""
    home = radio.center_hz
    radio.retune(center_hz=home, settle_s=SETTLE_S * 4)
    steady = _window_levels(radio.read(int(radio.rate_hz * SETTLE_SPAN_S)).samples,
                            SETTLE_WINDOW)
    if steady.size < 4:
        return {"measured": False, "detail": "too few slices to establish a settled level"}
    level = float(np.median(steady))
    # MAD rather than a standard deviation, and scaled to be comparable with one: a
    # single wild slice moves `std` enough to hide the transient it is measuring, and
    # the whole point is to be unmoved by outliers.
    sigma = float(np.median(np.abs(steady - level))) * 1.4826 or 1e-6
    tolerance = SETTLE_SIGMA * sigma
    slice_ms = SETTLE_WINDOW / radio.rate_hz * 1000.0
    hold = max(1, int(round(SETTLE_HOLD_S * 1000.0 / slice_ms)))
    took: list[float] = []
    span_ms = 0.0
    saturated = 0
    for _ in range(SETTLE_TRIALS):
        disturb()
        levels = _window_levels(
            radio.read(int(radio.rate_hz * SETTLE_SPAN_S)).samples, SETTLE_WINDOW
        )
        if levels.size == 0:
            continue
        span_ms = levels.size * slice_ms
        # SMOOTHED first: three sigma over several hundred slices puts a handful beyond
        # the line by chance, and a rule that trusts individual slices reported a whole
        # 62 ms block as a transient — measured off a fake that had none. A median over
        # five slices cannot be moved by an isolated one, and a transient never is.
        off = np.abs(_smooth(levels, SETTLE_SMOOTH) - level) > tolerance
        settled = _settled_index(off, hold)
        if settled is None:
            saturated += 1
            took.append(span_ms)
        else:
            took.append(settled * slice_ms)
    radio.retune(center_hz=home, settle_s=SETTLE_S)
    if not took:
        return {"measured": False, "detail": "the stream delivered nothing to time"}
    took.sort()
    return {
        "measured": True,
        "settle_ms": round(took[len(took) // 2], 3),
        "worst_ms": round(took[-1], 3),
        "configured_ms": round(SETTLE_S * 1000.0, 3),
        "window_us": round(SETTLE_WINDOW / radio.rate_hz * 1e6, 1),
        "steady_sigma_db": round(sigma, 3),
        "trials": len(took),
        # The two numbers that say whether the reading is a measurement at all: a settle
        # equal to the span is the stopwatch running out, not the radio settling.
        "span_ms": round(span_ms, 3),
        "hold_ms": round(hold * slice_ms, 3),
        "saturated": saturated,
    }


def _settle_after_retune(radio: Radio, other_hz: int) -> dict[str, Any]:
    """A whole hop: move away, come back, and flush — what the engine actually does."""
    home = radio.center_hz

    def disturb() -> None:
        radio.retune(center_hz=other_hz, settle_s=SETTLE_S * 4)
        radio.retune(center_hz=home, settle_s=0.0)

    return _settle_after(radio, disturb)


def _settle_after_barrier(radio: Radio, other_hz: int) -> dict[str, Any]:
    """What is LEFT of the transient after the REAL barrier has run.

    The check on the fix rather than on the radio, and the reason it exists is that the
    fix is a constant: `SETTLE_S` covers the measured worst case, but "measured" was
    seven trials on one radio on one band, and a constant that turns out to be short
    again would put the stale samples back silently.

    So: hop away, hop back through the barrier the engine actually uses, and time what is
    still disturbed. Near zero is the barrier doing its job. Tens of milliseconds is the
    constant being too small for this radio, whatever the frame rate says."""
    home = radio.center_hz

    def disturb() -> None:
        radio.retune(center_hz=other_hz, settle_s=SETTLE_S * 4)
        # No `settle_s`: the adaptive discard the engine actually uses.
        radio.retune(center_hz=home)

    return _settle_after(radio, disturb)


def _barrier_findings(left: dict[str, Any]) -> list[str]:
    """A barrier that stops early is a correctness fault wearing a frame rate's
    clothes — the picture gets quicker by being wrong, which is how the adaptive
    version passed for a whole 2.29 fps before its own fake convicted it."""
    if not left.get("measured"):
        return []
    worst = float(left.get("worst_ms") or 0.0)
    resolution = float(left.get("window_us") or 0.0) / 1000.0
    if worst > max(resolution, float(left.get("hold_ms") or 0.0)):
        return [
            f"the barrier STOPS EARLY: {worst} ms of the transient is still there "
            f"after it has run, so a hop still carries samples from the frequency it "
            f"came from — `SETTLE_S` is too small for this radio, and it is a "
            f"correctness fault rather than a slow one"
        ]
    return []


def _settle_after_flush(radio: Radio) -> dict[str, Any]:
    """The control: a flush and NO frequency change at all.

    A hop is a retune and a flush together, so every reading so far has been of both.
    If the transient is still here with the radio sitting exactly where it was, then
    `activateStream` is what costs the settle and the tuner is innocent — and the fix is
    to stop flushing every hop rather than to discard for longer after each one.

    Written because the previous suspect was falsified: the gain was already manual, and
    fixing it moved the median from 60.8 ms to 51.4, which is not a collapse."""
    def disturb() -> None:
        radio.barrier(0.0)  # the flush alone; `barrier` returns what it dropped

    return _settle_after(radio, disturb)


def _elsewhere(center_hz: int, rate_hz: int, direct: bool) -> int:
    """Somewhere far enough to be a real retune, and still somewhere this radio goes.

    A hop is a retune of about one capture's width, so the measurement should be one
    too — a settle measured across a ten-megahertz jump would be answering a question
    nobody asks of it."""
    away = center_hz + rate_hz
    ceiling = DIRECT_MAX_HZ if direct else MAX_TUNE_HZ
    return away if away < ceiling else center_hz - rate_hz


#: Settles to time a barrier at. Zero is the flush alone, so the ladder says outright
#: whether a discard is bounded by real time (it costs what it asks for) or by memcpy
#: (the stale samples are already captured and reading them is nearly free). Those two
#: answers call for opposite fixes, which is why guessing was not an option.
HOP_COST_SETTLES = (0.0, 0.05, 0.15)
#: Hops per settle. Enough that a median is not an anecdote, few enough that the probe
#: hands the radio back promptly.
HOP_COST_HOPS = 7


def _hop_cost(radio: Radio, other_hz: int, want: int) -> dict[str, Any]:
    """Where a hop's wall clock actually goes, phase by phase.

    MEASURED on the box, 2026-09-05: the FM dial hops eleven times per row and manages
    1.0 fps, so a hop costs about 90 ms — while the samples it keeps are 1024, which is
    0.4 ms of signal. Something other than the signal is the entire frame rate.

    `retune` is two operations with very different costs — a USB control transfer, then
    a discard that may or may not be bounded by real time — so they are timed apart. A
    fix aimed at the wrong phase would cost a deploy and learn nothing."""
    home = radio.center_hz
    tune: list[float] = []
    read: list[float] = []
    barrier: dict[str, list[float]] = {str(int(s * 1000)): [] for s in HOP_COST_SETTLES}
    step = 0
    for _ in range(HOP_COST_HOPS):
        for settle in HOP_COST_SETTLES:
            # Alternate, so every timing is a real move rather than a retune to where
            # the radio already is — which the driver may well make free.
            target = other_hz if step % 2 == 0 else home
            step += 1
            start = time.perf_counter()
            radio._apply(center_hz=target)
            tuned = time.perf_counter()
            radio.barrier(settle)
            barred = time.perf_counter()
            radio.read(want)
            done = time.perf_counter()
            tune.append((tuned - start) * 1000.0)
            barrier[str(int(settle * 1000))].append((barred - tuned) * 1000.0)
            read.append((done - barred) * 1000.0)
    radio.retune(center_hz=home)
    paid = {key: _median_ms(got) for key, got in barrier.items()}
    return {
        "hops": step,
        "want_samples": want,
        "tune_ms": _median_ms(tune),
        "read_ms": _median_ms(read),
        "barrier_ms": paid,
        # The interpretation, in the payload rather than in `findings`: both answers
        # describe a healthy radio, and they call for opposite fixes.
        "bound": _hop_bound(paid),
    }


def _median_ms(got: list[float]) -> float:
    return round(float(np.median(got)), 3) if got else 0.0


def _hop_bound(barrier: dict[str, float]) -> str:
    """Which of the two opposite fixes the discard's cost calls for.

    NOT a finding: both answers are healthy radios, and the probe's `findings` list
    means a claim did not hold. This is the reading itself.

    `real-time` — the settle costs what it asks for, so every millisecond of it comes
    off every hop and discarding longer cannot be the fix; having less stale data has
    to be. `memcpy` — the stale samples are already captured and reading them is nearly
    free, so the settle can cover the worst case without costing frames."""
    asked = 50.0
    paid = float(barrier.get("50") or 0.0)
    return "real-time" if paid >= asked * 0.5 else "memcpy"


def _hop_dominant(cost: dict[str, Any]) -> str:
    """The phase that owns a hop before any discard — eleven of it is every FM row."""
    barrier = cost.get("barrier_ms") or {}
    phases = [
        (float(barrier.get("0") or 0.0), "flush"),
        (float(cost.get("tune_ms") or 0.0), "setFrequency"),
        (float(cost.get("read_ms") or 0.0), "read"),
    ]
    return max(phases)[1]


def _settle_findings(settle: dict[str, Any]) -> list[str]:
    """Say it when the constant and the radio disagree, in the direction it matters.

    TOO SHORT is a correctness fault: the first samples of every hop are then the
    previous hop's tail, and a waterfall built from them draws signals at frequencies
    they are not on. TOO LONG is only a speed fault, but it is the one that decides
    whether a wide band is watchable."""
    measured = float(settle["settle_ms"])
    configured = float(settle["configured_ms"])
    worst = float(settle.get("worst_ms") or measured)
    # A difference smaller than one slice is not a difference: that is the resolution
    # this measurement HAS, and reporting inside it would be reporting the method.
    resolution = float(settle.get("window_us") or 0.0) / 1000.0
    # Said FIRST, because a saturated reading is not a number: the level never went
    # quiet inside the block, so the settle is "at least the span" and the span is a
    # property of this measurement. Reporting it as a measured figure would be
    # reporting the stopwatch.
    if settle.get("saturated"):
        return [
            f"the retune settle did NOT finish inside the {settle.get('span_ms')} ms "
            f"this measurement watches, on {settle['saturated']} of "
            f"{settle.get('trials')} trials: it is AT LEAST that against "
            f"{configured} ms configured, so a hop can carry the previous hop's "
            f"samples and draw them at the wrong frequency — and the real figure is "
            f"still unknown, because the span has to exceed it to measure it"
        ]
    if worst > configured + resolution:
        return [
            f"the retune settle is too SHORT: the radio needed {worst} ms at worst and "
            f"{configured} ms is configured, so a hop can carry the previous hop's "
            f"samples and draw them at the wrong frequency"
        ]
    if measured * 4 < configured:
        return [
            f"the retune settle is {configured} ms and the radio needed {measured} — "
            f"every hop pays the difference, so a wide band redraws several times "
            f"slower than this radio can manage"
        ]
    return []


def _gain_findings(gain: dict[str, Any]) -> list[str]:
    """Nothing in this engine ever sets the gain, so whatever it is, it is a default."""
    if gain.get("automatic"):
        return [
            "the tuner's gain is AUTOMATIC — nothing in this engine ever sets it. A "
            "waterfall's dB scale then means nothing from row to row, and every hop "
            "seam is a gain step drawn as if the band had changed."
        ]
    at = gain.get("gain_db")
    floor = gain.get("gain_min_db")
    if at is None or floor is None:
        return []
    if float(at) <= float(floor):
        return [
            f"the tuner's gain is fixed at {at} dB, the BOTTOM of its "
            f"{floor}-{gain.get('gain_max_db')} dB range — nothing here ever sets it, "
            f"so that is a default rather than a choice, and it costs every weak "
            f"signal on every band"
        ]
    return []


def _flush_findings(hop: dict[str, Any], flush: dict[str, Any]) -> list[str]:
    """Say it when the flush, not the tuner, is what a hop is waiting for."""
    if not (hop.get("measured") and flush.get("measured")):
        return []
    moved = float(hop["settle_ms"])
    still = float(flush["settle_ms"])
    if still >= moved * 0.5:
        return [
            f"the transient is the FLUSH, not the retune: a flush with no frequency "
            f"change still disturbs the output for {still} ms against {moved} ms for a "
            f"whole hop, so `activateStream` is what every hop is waiting for and "
            f"discarding longer after each one cannot be the fix"
        ]
    return []


def _selection(radio: Radio, found: list[dict[str, str]]) -> dict[str, Any]:
    """Did `serial=` open the radio it named, or whichever one enumerated first?

    SoapyRTLSDR's `getHardwareInfo` carries the device INDEX rather than the serial, and
    enumeration carries both — so the two together answer the question, where either on
    its own only looks like it does."""
    serials = [entry.get("serial", "") for entry in found]
    wanted = radio.serial
    out: dict[str, Any] = {"count": len(found), "serials": serials}
    if wanted is None:
        out["selects_by_serial"] = None
        out["detail"] = "no serial asked for, so nothing was selected by one"
        return out
    index = str(radio.hardware_info().get("index", ""))
    expected = [
        str(i) for i, entry in enumerate(found) if entry.get("serial") == wanted
    ]
    out["asked_for"] = wanted
    out["opened_index"] = index
    out["enumeration_index"] = expected[0] if expected else None
    if not index:
        # A driver that carries no `index` cannot answer the question either way, and a
        # "did not select" verdict it never earned would be a false alarm on the one
        # claim everything per-radio here depends on.
        out["selects_by_serial"] = None
        out["detail"] = "the driver reported no device index to check the choice against"
        return out
    out["selects_by_serial"] = bool(expected) and index == expected[0]
    return out


def probe(
    *,
    serial: str | None = None,
    center_hz: int = PROBE_CENTER_HZ,
    rate_hz: int = PROBE_RATE_HZ,
    bins: int = PROBE_BINS,
    driver: Driver | None = None,
) -> dict[str, Any]:
    """Answer F0's questions against a real dongle, and return a VERDICT, not a dump.

    Every one of these is a claim this engine is already written against, and each is
    the kind only hardware can retire (§7, F0): does SoapySDR enumerate, does `serial=`
    select a named radio, does `direct_samp=2` take, do `setFrequency` and
    `setSampleRate` work ON A LIVE STREAM with no rebuild, is `SOAPY_SDR_OVERFLOW`
    really reported under backpressure, what is the achieved rate against the requested
    one, and — the one nothing else can answer — did `bufflen` actually take.

    Each check is caught on its own: a probe that stops at the first failure answers one
    question and hides six, and the whole point of running it is to come back with the
    full list."""
    try:
        return _probe(
            serial=serial, center_hz=center_hz, rate_hz=rate_hz, bins=bins, driver=driver
        )
    except Exception as broke:  # noqa: BLE001 - a verdict is the whole contract
        # A probe that raises answers NOTHING, and its message then has to survive the
        # api, the tunnel and the edge to be read — which MEASURED 2026-09-05 it does
        # not: a 502 reached the console as Cloudflare's own error page with the
        # sentence stripped, so the one call shape that failed was invisible from the
        # only surface the owner has (CLAUDE.md #10). Every escape becomes a finding
        # here, and the traceback goes to the log where `logs sdr` can reach it.
        _log(f"probe failed: {traceback.format_exc()}")
        return {
            "ok": False,
            "serial": serial,
            "summary": f"The probe itself failed: {type(broke).__name__}: {broke}",
            "findings": [
                f"{type(broke).__name__}: {broke}",
                "This is the probe's own code, not a verdict about the radio — the "
                "traceback is in `logs sdr`.",
            ],
            "traceback": traceback.format_exc()[-1500:],
        }


def _probe(
    *,
    serial: str | None,
    center_hz: int,
    rate_hz: int,
    bins: int,
    driver: Driver | None,
) -> dict[str, Any]:
    started = time.monotonic()
    drv = driver if driver is not None else _Soapy()
    out: dict[str, Any] = {
        "serial": serial,
        "soapy": _version_or_error(drv),
        "requested": {
            "center_hz": center_hz,
            "rate_hz": rate_hz,
            "bins": bins,
            "direct_sampling": center_hz < DIRECT_MAX_HZ,
        },
    }
    try:
        found = drv.enumerate({"driver": "rtlsdr"})
    except Exception as failed:  # noqa: BLE001 - see the wrapper above
        out["ok"] = False
        out["summary"] = f"SoapySDR could not enumerate: {type(failed).__name__}: {failed}"
        out["findings"] = [f"enumerate() raised {type(failed).__name__}: {failed}"]
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        return out
    out["enumerate"] = {"count": len(found), "devices": found}
    if not found:
        out["ok"] = False
        out["summary"] = "SoapySDR enumerated no rtlsdr device at all."
        out["findings"] = ["Nothing to probe: check `GET /api/debug/sdr` for the bus."]
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        return out

    direct = center_hz < DIRECT_MAX_HZ
    try:
        return _probe_open(
            out, drv, found, started, serial, center_hz, rate_hz, bins, direct
        )
    except RadioError as shut:
        # Opening is the one step whose failure used to cost the enumeration too: the
        # wrapper in `probe` catches, builds a fresh verdict, and the device list that
        # would say WHY the open found no match goes with it. MEASURED 2026-09-05 —
        # `make() no match` against a filter of `{"driver": "rtlsdr"}`, with the list of
        # what enumeration had just found nowhere in the answer. Keep it.
        out["ok"] = False
        out["summary"] = f"Enumeration found {len(found)}, but the radio would not open: {shut}"
        out["findings"] = [
            str(shut),
            "Enumeration and `make` disagree — compare `enumerate.devices` below "
            "against the filter in the message: a wrong filter and an unloaded driver "
            "look identical from the error alone and have opposite fixes.",
        ]
        # The filter provably matches the enumeration and the open still fails, so ask
        # the same question three ways — what this process has LOADED, what `enumerate`
        # says for the exact args `make` rejects, and every call shape — and let the
        # measurement pick the fix instead of a fifth guess.
        diagnose = getattr(drv, "open_diagnosis", None)
        if diagnose is not None:
            asked = {"driver": "rtlsdr"} | ({"serial": serial} if serial else {})
            out["open_diagnosis"] = diagnose(asked)
            out["findings"].append(_diagnosis_finding(out["open_diagnosis"]))
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        return out


SOAPY_UTIL = "/usr/bin/SoapySDRUtil"
UTIL_TIMEOUT_S = 20.0


def _soapy_util(*argv: str) -> str:
    """Ask the C++ tool the same question, in a process of its own.

    `soapysdr-tools` is already in the image (Dockerfile.sdr). It links the same
    libSoapySDR and loads the same modules, with none of the Python binding in front of
    it — so it separates "this process is wrong" from "this image is wrong", which is
    the one axis nothing else here can test. A different answer from the two would be
    the finding; the same answer moves the fault below both.

    Never a shell, and the arguments are ours: this runs as root beside the radio."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no caller input
            [SOAPY_UTIL, *argv],
            capture_output=True,
            text=True,
            timeout=UTIL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as failed:
        return f"{type(failed).__name__}: {failed}"[:200]
    said = (done.stdout + done.stderr).strip()
    return f"rc={done.returncode} {said}"[:1200]


def _linked_soapy() -> list[str]:
    """Which libSoapySDR this process actually mapped, read from its own memory map.

    Two of them — a distro binding against one library and something else against
    another — would make `enumerate` and `make` genuinely different objects, and is the
    only hypothesis left that explains a find function without a make function. It
    costs one file read to rule in or out, and a whole deploy to discover it missing."""
    try:
        with open("/proc/self/maps", encoding="utf-8") as maps:
            found = {
                line.rsplit(" ", 1)[-1].strip()
                for line in maps
                if "libSoapySDR" in line or "_SoapySDR" in line
            }
    except OSError as unreadable:  # pragma: no cover - /proc is always there in the image
        return [f"unreadable: {unreadable}"]
    return sorted(found)


CONTROL_FILTER = "a driver name that does not exist (control)"
"""The filter that MUST fail, so that a failure means something when it happens."""


def _diagnosis_finding(diag: dict[str, Any]) -> str:
    """One sentence naming what the diagnosis actually separated.

    Ordered by how the fixes differ: a missing module is a packaging fault, a filter
    that opens is a code fault, and `enumerate` and `make` disagreeing over the same
    args is neither — it is SoapySDR contradicting itself, and the next place to look
    is inside `make` rather than at anything this file passes it."""
    filters = [row for row in diag.get("filters", []) if isinstance(row, dict)]
    by_name = {row.get("filter"): row for row in filters}
    # An open that HAPPENED outranks every inference about why the others did not.
    # MEASURED: the control-matches reading fired on a box where the driver was
    # registered and two other filters opened, and it was simply false. A filter or a
    # shape that works is a fix; "no factory registered" was a story about the evidence.
    opened = [
        row["filter"] for row in filters if row.get("make") == "opened" and row["filter"]
    ]
    if opened:
        return f"A filter that DID open it: {opened[0]}."
    control = by_name.get(CONTROL_FILTER, {}).get("make")
    named = by_name.get("driver + serial") or by_name.get("driver only") or {}
    if control and control != "opened" and named.get("make") == control:
        return (
            "Every filter tried fails EXACTLY as a driver name that does not exist "
            "does, and none opened — so no rtlsdr factory answered in this process."
        )
    environment = diag.get("environment", {})
    broken = environment.get("module_errors")
    if isinstance(broken, dict):
        refused = [f"{path}: {why}" for path, why in broken.items() if "rtl" in path.lower()]
        if refused:
            return (
                f"The rtlsdr module loaded and registered NOTHING — {refused[0]}. That "
                "is why the same args enumerate and will not open."
            )
    modules = environment.get("modules")
    if isinstance(modules, list) and not any("rtl" in m.lower() for m in modules):
        return (
            "No rtlsdr module is loaded in this process — enumeration is answering "
            "from somewhere `make` never looks, so this is packaging, not code."
        )
    listing = [
        row["filter"] for row in filters
        if str(row.get("enumerate", "")).isdigit() and int(row["enumerate"]) > 0
    ]
    if listing:
        return (
            f"`{listing[0]}` enumerates and will not open — the same args answered two "
            "ways, so the fault is inside make(), not in what this file passes it."
        )
    return "Nothing enumerated under any filter — the driver or the bus, not the binding."


def _probe_open(
    out: dict[str, Any],
    drv: Driver,
    found: list[dict[str, str]],
    started: float,
    serial: str | None,
    center_hz: int,
    rate_hz: int,
    bins: int,
    direct: bool,
) -> dict[str, Any]:
    findings: list[str] = []
    with Radio.open(
        rate_hz=rate_hz,
        center_hz=center_hz,
        serial=serial,
        direct=direct,
        driver=drv,
        doing="probing the radio",
    ) as radio:
        out["selection"] = _answered("selection", lambda: _selection(radio, found))
        if out["selection"].get("selects_by_serial") is False:
            findings.append(
                "`serial=` did NOT open the radio it named — every per-radio claim "
                "in this system depends on it doing so."
            )

        # The branch, read back rather than assumed: `writeSetting` returns nothing, so
        # a mode the driver declined would otherwise look identical to one it took.
        branch: dict[str, Any] = {"requested": DIRECT_Q_BRANCH if direct else DIRECT_OFF}
        try:
            branch["read_back"] = radio.read_setting(DIRECT_SAMP)
        except Exception as failed:  # one failed check is not a failed probe
            branch["error"] = str(failed)
        branch["works"] = branch.get("read_back") == branch["requested"]
        out["direct_samp"] = branch
        if direct and not branch["works"]:
            findings.append(
                "`direct_samp=2` did not read back as 2 — shortwave through the Q "
                "branch is the claim this engine's HF half rests on."
            )

        out["bufflen"] = _answered("bufflen", lambda: _callback_period(radio))
        if not out["bufflen"].get("took"):
            findings.append(
                f"`bufflen` did NOT take: buffers measured "
                f"{out['bufflen'].get('callback_ms')} ms against "
                f"{out['bufflen'].get('expected_ms')} ms wanted, which is librtlsdr's "
                f"silent fallback to {DEFAULT_BUFFLEN_BYTES} bytes."
            )

        out["capture"] = _answered("capture", lambda: _capture(radio, bins))

        # The one number a hopping sweep is made of. Measured rather than assumed
        # because `SETTLE_S` was chosen, and a hop pays it once per hop: at 50 ms an
        # eleven-hop FM sweep cannot beat two rows a second however fast the samples
        # arrive, and at 5 ms it manages fifteen.
        out["retune_settle"] = _answered(
            "retune_settle",
            lambda: _settle_after_retune(radio, _elsewhere(center_hz, rate_hz, direct)),
        )
        settle = out["retune_settle"]
        if settle.get("measured") and settle.get("settle_ms") is not None:
            findings.extend(_settle_findings(settle))

        out["gain"] = _answered("gain", radio.gain_state)
        findings.extend(_gain_findings(out["gain"]))

        # Is the settle a PLL relock or a gain loop re-converging? An R820T2 locks in
        # well under a millisecond, and what the stopwatch watches is a LEVEL.
        # FALSIFIED on the box, 2026-09-05: the gain was already manual, and fixing it
        # moved the median from 60.8 ms to 51.4 — not a collapse. Kept because a
        # falsified suspect that is not re-checked comes back.
        out["retune_settle_fixed_gain"] = _answered(
            "retune_settle_fixed_gain",
            lambda: _settle_fixed_gain(radio, _elsewhere(center_hz, rate_hz, direct)),
        )

        # The control that separates the hop's two halves. If the transient survives a
        # flush with NO frequency change, `activateStream` owns it and the tuner does
        # not — and the fix is to stop flushing every hop, not to discard for longer.
        out["settle_after_flush"] = _answered(
            "settle_after_flush", lambda: _settle_after_flush(radio)
        )
        findings.extend(_flush_findings(out["retune_settle"], out["settle_after_flush"]))

        # And the check on the FIX: what is left of the transient once the adaptive
        # discard the engine really uses has run. Near zero is the discard working.
        out["settle_after_barrier"] = _answered(
            "settle_after_barrier",
            lambda: _settle_after_barrier(
                radio, _elsewhere(center_hz, rate_hz, direct)
            ),
        )
        findings.extend(_barrier_findings(out["settle_after_barrier"]))

        # And where the rest of a hop's time goes. The settle says how much stale data
        # there is; this says what removing it COSTS, and the two together are the only
        # way to choose between discarding longer and having less to discard.
        out["hop_cost"] = _answered(
            "hop_cost",
            lambda: _hop_cost(radio, _elsewhere(center_hz, rate_hz, direct), bins * 4),
        )
        if out["hop_cost"].get("hops"):
            out["hop_cost"]["dominant_phase"] = _hop_dominant(out["hop_cost"])

        # A retune ON THE LIVE STREAM, with the stream handle checked for identity
        # either side: a rebuild would hand back a different one.
        before = radio.stream_token
        moved_hz = center_hz + rate_hz
        live: dict[str, Any] = {"from_hz": center_hz, "to_hz": moved_hz}
        try:
            live["discarded"] = radio.retune(center_hz=moved_hz)
            live["capture"] = _answered("retuned capture", lambda: _capture(radio, bins))
            live["stream_rebuilt"] = radio.stream_token != before
            live["works"] = not live["stream_rebuilt"]
        except Exception as failed:  # noqa: BLE001 - one claim, not the probe
            live["works"] = False
            live["error"] = f"{type(failed).__name__}: {failed}"
        out["live_retune"] = live
        if not live.get("works"):
            findings.append(
                "A frequency change on a live stream failed — pan without blanking is "
                "the reason SoapySDR was chosen over an `rtl_sdr` pipe."
            )

        rates: list[dict[str, Any]] = []
        for wanted in PROBE_RATES:
            row: dict[str, Any] = {"requested": wanted}
            try:
                radio.retune(rate_hz=wanted)
                row["achieved"] = radio.achieved_rate_hz
                row["exact"] = radio.achieved_rate_hz == float(wanted)
                row["warning"] = radio.rate_warning
                row["stream_rebuilt"] = radio.stream_token != before
                row["samples"] = radio.read(radio.samples_per_buffer).samples.size
                row["works"] = not row["stream_rebuilt"]
            except Exception as failed:  # noqa: BLE001 - one rate, not the probe
                row["works"] = False
                row["error"] = f"{type(failed).__name__}: {failed}"
            rates.append(row)
        out["rates"] = rates
        if not all(row.get("works") for row in rates):
            findings.append(
                "A rate change on a live stream failed — zoom would rebuild the "
                "stream, which is the asterisk this `bufflen` exists to remove."
            )
        inexact = [row["requested"] for row in rates if row.get("exact") is False]
        if inexact:
            findings.append(
                f"Rates that do NOT come back off the divider unchanged: {inexact}. "
                f"`start_hz`/`bin_hz` must stay derived from the requested integers."
            )

        # Backpressure, induced rather than waited for: stop reading until the driver's
        # ring is several times over, then read and see whether it says so. `rtl_sdr`
        # had no such signal — the drop was silent — so this is the check that decides
        # whether an overrun can ever be surfaced to a viewer.
        time.sleep(PROBE_BACKPRESSURE_S)
        starved = radio.read(radio.samples_per_buffer)
        out["overflow"] = {
            "starved_for_s": PROBE_BACKPRESSURE_S,
            "reported": starved.overflows > 0,
            "overflows": starved.overflows,
            "timeouts": starved.timeouts,
            "reads": starved.reads,
        }
        if not out["overflow"]["reported"]:
            findings.append(
                "No `SOAPY_SDR_OVERFLOW` after a second of induced backpressure — "
                "then an overrun is as silent here as it was under `rtl_sdr`."
            )

    peak = out.get("capture", {})
    if peak.get("settled"):
        findings.append(
            "The FIRST frame after the branch was switched had nothing in it and a "
            "later one did, so a reading taken immediately here means nothing. Anything "
            "that captures one frame and judges it — including this probe before "
            "2026-09-05 — will call this radio deaf when it is not."
        )
    if peak.get("dead"):
        findings.append(
            f"Nothing reached the ADC at {peak.get('center_hz')} Hz in "
            f"{peak.get('frames')} frames: a DC spike at {peak.get('dc_db')} dBFS with "
            "no noise floor under it. A receiver with no noise floor is not receiving, "
            "and after this many frames it is not a settle — look at the antenna and, "
            "under `direct_samp`, at whether the board wires that branch at all."
        )
    out["findings"] = findings
    out["ok"] = not findings
    reading = (
        f"nothing but a DC spike at {peak.get('dc_db')} dBFS"
        if peak.get("dead")
        else (
            f"peak {peak.get('peak_hz')} Hz at {peak.get('peak_db')} dBFS, "
            f"{peak.get('above_floor_db')} dB over the frame's own floor"
        )
    )
    out["summary"] = (
        f"{len(found)} radio(s); {'every' if not findings else 'not every'} F0 claim "
        f"held. Buffers {out['bufflen'].get('callback_ms')} ms "
        f"(wanted {out['bufflen'].get('expected_ms')}); {reading}."
    )
    out["elapsed_s"] = round(time.monotonic() - started, 2)
    return out
