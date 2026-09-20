"""Watching a panel's own console over the USB it was flashed through.

THE GAP THIS CLOSES. Everything else here reports what the BOX saw. When a panel polled
the manifest and then quietly did not update, the access log showed a 200 and nothing
else — identical, from the box's side, to a panel that was correctly up to date. The
reason was on the panel, in a log line nobody could read, and the owner has no terminal
to read it with (CLAUDE.md #10). A flashed unit was a black box the moment it left the
flasher.

It costs nothing to listen: the same `/dev/ttyACM0` this container already opens to flash
carries the firmware's console, because the S3's native USB is both. No second cable, no
UART header, no change to the firmware.

RESETTING IS A FEATURE, not a side effect to avoid. A panel checks for firmware every 15
minutes, so watching a running one means waiting up to a quarter of an hour for the
interesting line. A reset makes the whole boot sequence — Wi-Fi, TLS, the manifest fetch,
the version comparison, an OTA attempt — happen in the first few seconds. So the reset is
offered explicitly, and NOT doing it is the default: an unasked-for reset of a panel on a
child's wall is the kind of surprise this surface should not have.

LETTING GO IS THE DANGEROUS PART, and this is the bug that taught it. Opening is careful —
DTR and RTS are set false BEFORE open, precisely so listening does not restart anything —
and then `close()` was left to do whatever a Linux tty close does, which is drop both
lines. On this chip those lines are not bookkeeping: RTS is reset and DTR is the boot pin,
so the wrong transition on the way out leaves the panel sitting in the ROM bootloader with
the application never started. From the room that is a dead black screen that stays dead
until someone unplugs it — which is exactly what the owner saw, and exactly what he worked
out, after a session of me reading these logs and blaming the display.

So a watch now ENDS by pulsing the panel back into its application. That costs a reboot
nobody asked for, which the paragraph above calls a surprise worth avoiding, and it is
still the right trade: a two-second restart against a panel that is dark until someone
finds it and pulls the cable. Being sure of the state we leave behind beats being clever
about not disturbing it.

THE PORT IS EXCLUSIVE. Only one process can hold a tty, and a flash matters more than a
watch, so a monitor registers itself here and `/flash` asks it to let go first. Getting
that backwards would mean the owner pressing Flash and being told the device is busy by
their own debugging tool.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator

# USB CDC-ACM ignores the line rate entirely — the S3's native USB is not a UART — but
# pyserial requires one, and this is the value every Espressif tool prints.
BAUD = 115200

# A read timeout short enough that a stop request is noticed promptly, long enough not to
# spin. The loop wakes this often whether or not the panel said anything.
READ_TIMEOUT_S = 0.25

# Bounds, because this streams to a phone. A panel in a boot loop can produce output far
# faster than anyone can read it, and an unbounded watch would be a way to fill a screen
# and a network link with the same twenty lines.
MAX_SECONDS = 900
MAX_LINES = 5000
MAX_LINE_CHARS = 512

# Reset pulse. On the S3's USB Serial/JTAG, RTS drives chip reset and DTR drives the boot
# pin; holding RTS alone restarts into the normal application rather than the bootloader.
RESET_HOLD_S = 0.12

_lock = threading.Lock()
_active: dict[str, threading.Event] = {}


def decode_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Bytes off the wire to whole console lines.

    Split out from the serial port so the thing most likely to be subtly wrong — a line
    arriving in two reads, a CRLF, a half-finished line when the watch ends — is testable
    on a machine with no panel attached to it.

    Decoded with `replace` rather than strictly: the first bytes after a reset are the ROM
    bootloader talking at a different line rate, and arriving as mojibake is correct. It
    is a real thing the panel said, and hiding it would hide the evidence that a reset
    happened at all.
    """
    buf = bytearray()
    for chunk in chunks:
        buf.extend(chunk)
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            line, buf = buf[:nl], buf[nl + 1 :]
            yield line.decode("utf-8", "replace").rstrip("\r")[:MAX_LINE_CHARS]
    if buf:
        yield buf.decode("utf-8", "replace").rstrip("\r")[:MAX_LINE_CHARS]


def release(port: str, timeout: float = 3.0) -> bool:
    """Ask any monitor on `port` to stop, and wait for it to actually let go.

    Returns whether the port is free. A flash calls this first: the tty is exclusive, and
    the owner pressing Flash must never lose to their own log window.
    """
    with _lock:
        stop = _active.get(port)
    if stop is None:
        return True
    stop.set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if port not in _active:
                return True
        time.sleep(0.05)
    with _lock:
        return port not in _active


def _open(port: str):  # type: ignore[no-untyped-def] - pyserial ships no stubs
    # Imported here so the pure half above needs no device library. pyserial ships no
    # type information, which is not a finding worth carrying on every run.
    import serial  # pyright: ignore[reportMissingModuleSource]

    # Configured BEFORE open. pyserial asserts DTR/RTS as it opens a port, which on this
    # chip is a reset line — so opening with the defaults would restart a panel that the
    # caller only asked to listen to.
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = READ_TIMEOUT_S
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def watch(port: str, *, seconds: int, reset: bool) -> Iterator[str]:
    """Stream a panel's console until the time runs out or a flash asks for the port."""
    seconds = max(1, min(int(seconds), MAX_SECONDS))
    stop = threading.Event()
    with _lock:
        if port in _active:
            raise RuntimeError(f"{port} is already being watched")
        _active[port] = stop

    try:
        try:
            ser = _open(port)
        except Exception as exc:  # noqa: BLE001 - the stream carries the reason
            raise RuntimeError(f"could not open {port}: {exc}") from exc

        try:
            if reset:
                yield "-- restarting the panel --"
                ser.rts = True
                time.sleep(RESET_HOLD_S)
                ser.rts = False

            deadline = time.monotonic() + seconds
            lines = 0

            def _chunks() -> Iterator[bytes]:
                while not stop.is_set() and time.monotonic() < deadline:
                    data = ser.read(4096)
                    if data:
                        yield data

            for line in decode_lines(_chunks()):
                yield line
                lines += 1
                if lines >= MAX_LINES:
                    yield f"-- stopped after {MAX_LINES} lines --"
                    return
            yield "-- stopped: the panel is still running --" if not stop.is_set() else (
                "-- stopped: the port was needed for a flash --"
            )
        finally:
            # Never leave the chip's boot pin to chance — see the module docstring.
            try:
                ser.dtr = False
                ser.rts = True
                time.sleep(RESET_HOLD_S)
                ser.rts = False
            except Exception:  # noqa: BLE001 - a port that already went away is not news
                pass
            ser.close()
    finally:
        with _lock:
            _active.pop(port, None)
