"""Putting MicroPython and the GrowBot firmware on a Pico 2 W (docs/plans/GROWBOT_PLAN.md §3).

The same split as `flash.py`: what is run against a board (every argv, every on-device
script) is built by pure functions the tests can read, and the half that spawns processes is
a set of generators that stream log lines and end with `OK …` or `FAILED: <code>: <message>`.

`mpremote` and `picotool` run as command-line subprocesses only, never imported: they are the
tools upstream documents, and a library call would tie this file to their internals.

WHAT REACHES A DEVICE. The api names a port; this process runs as root with `/dev` mounted,
so a port is used only if the sysfs scan says it is a Raspberry Pi device (vid 2e8a), and
picotool is only ever handed the bus and address of a sysfs-verified RP2350 bootloader
(2e8a:000f), never a path or a vid/pid from the request. The on-device scripts are fixed text
in this file; the one value that enters one (a rekey's key) is held to a regex first and goes
in a temporary file, never on a command line.

WHY A RESET COMES FIRST. A running robot arms the RP2350's hardware watchdog once it reaches
the box (relay_chip.py, 8 s, never disarmed). mpremote stops main.py to copy files, the
watchdog then stops being fed, and a copy that runs past it is cut off by a reboot. A hard
reset clears the watchdog, and mpremote interrupts the fresh main.py within a second or two,
long before it joins Wi-Fi and arms it again.

NEVER 1200 BAUD. MicroPython's rp2 port reboots into the bootloader when its CDC port is
opened at 1200 baud (the Arduino "touch"), so the console is opened at 115200 like any other.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import monitor
import ports

MPREMOTE = "mpremote"
PICOTOOL = "picotool"

NAME_RE = re.compile(r"^[A-Za-z0-9_]+\.py$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 12
MAX_FILE_BYTES = 256 * 1024
# wifi_store.MAX_CONFIG: a larger secrets.py is one the firmware refuses to read.
MAX_SECRETS_BYTES = 16384
MAX_WATCH_S = 120

BOOT_WAIT_S = 10.0
TTY_WAIT_S = 20.0
RESET_WAIT_S = 10.0
# After WIFI_OK the robot dials the box; its handshake line is worth waiting a little for,
# but the api decides whether it arrived (the relay stamps last_seen_at).
HANDSHAKE_WAIT_S = 30.0
POLL_S = 0.25
# A USB device that just reset is still listed for a moment before it drops off the bus.
SETTLE_S = 1.0
COMMAND_TIMEOUT_S = 180.0
BAUD = 115200

# Printed by the identify script on one line, so console noise around it cannot be misread.
IDENTIFY_MARK = "GBID "
REKEY_OK = "GBREKEY OK"

IDENTIFY_SCRIPT = (
    "import binascii, json, machine, os, sys\n"
    "v = sys.implementation.version\n"
    "print('GBID ' + json.dumps({"
    "'code': 'gb-' + binascii.hexlify(machine.unique_id()).decode()[-6:], "
    "'micropython': '%d.%d.%d' % (v[0], v[1], v[2]), "
    "'machine': os.uname().machine}))\n"
)

# Swaps the one GROWBOT_KEY line of secrets.py and nothing else, through a temporary file
# and a rename, so a power cut mid-write leaves the old secrets.py whole.
REKEY_SCRIPT = """import os
KEY = "{key}"
with open("secrets.py") as f:
    lines = f.read().split("\\n")
hits = [i for i, line in enumerate(lines) if line.startswith("GROWBOT_KEY = ")]
if len(hits) != 1:
    raise SystemExit("GBREKEY NOKEY")
lines[hits[0]] = 'GROWBOT_KEY = "' + KEY + '"'
with open("secrets.py.tmp", "w") as f:
    f.write("\\n".join(lines))
os.rename("secrets.py.tmp", "secrets.py")
print("GBREKEY OK")
"""

_busy = threading.Lock()


class PicoError(RuntimeError):
    """A failure with one of the codes the api maps to a recovery sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# -- argv and scripts (pure) -------------------------------------------------------------


def bootloader_argv(port: str) -> list[str]:
    return [MPREMOTE, "connect", port, "bootloader"]


def reset_argv(port: str) -> list[str]:
    return [MPREMOTE, "connect", port, "reset"]


def identify_argv(port: str) -> list[str]:
    return [MPREMOTE, "connect", port, "exec", IDENTIFY_SCRIPT]


def picotool_argv(bus: int, address: int, uf2: str) -> list[str]:
    """`-v` reads the flash back against the image; `-x` starts it. The device is chosen by
    the bus and address the kernel gave a verified bootloader, so picotool can never pick
    some other RP-series device on its own."""
    return [
        PICOTOOL,
        "load",
        "-v",
        "-x",
        uf2,
        "-t",
        "uf2",
        "--bus",
        str(int(bus)),
        "--address",
        str(int(address)),
    ]


def copy_argv(port: str, pairs: Iterable[tuple[str, str]]) -> list[str]:
    """One mpremote session for every file, chained with `+`, so main.py is stopped once."""
    argv = [MPREMOTE, "connect", port]
    for i, (local, remote) in enumerate(pairs):
        if i:
            argv.append("+")
        argv += ["cp", local, f":{remote}"]
    return argv


def run_argv(port: str, script: str) -> list[str]:
    return [MPREMOTE, "connect", port, "run", script]


def rekey_script(key: str) -> str:
    if not KEY_RE.fullmatch(key):
        raise PicoError("internal", "the key is not a device key")
    return REKEY_SCRIPT.format(key=key)


# -- validation ----------------------------------------------------------------------------


def pico_port(port: object) -> str:
    """`port` if the sysfs scan lists it as a Raspberry Pi tty, else ValueError."""
    known = {p.device: p for p in ports.scan()}
    found = known.get(str(port or ""))
    if found is None or not found.is_pico:
        picos = sorted(d for d, p in known.items() if p.is_pico)
        raise ValueError(
            f"{port!r} is not a Pico on this box; Picos: {picos or 'none'}"
        )
    return found.device


def decode_files(raw: object) -> list[tuple[str, bytes]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("files must be a non-empty list")
    if len(raw) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} files")
    out: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each file must be an object")
        name = str(item.get("name") or "")
        # Flat names only: the regex has no slash, dot segment or drive.
        if not NAME_RE.fullmatch(name) or name == "secrets.py" or name in seen:
            raise ValueError(f"bad file name {name!r}")
        seen.add(name)
        data = decode_b64(item.get("b64"), name)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name} is over {MAX_FILE_BYTES} bytes")
        out.append((name, data))
    return out


def decode_secrets(raw: object) -> bytes:
    data = decode_b64(raw, "secrets")
    if not data or len(data) > MAX_SECRETS_BYTES:
        raise ValueError("secrets.py is empty or too large")
    return data


def decode_b64(raw: object, what: str) -> bytes:
    try:
        return base64.b64decode(str(raw or ""), validate=True)
    except ValueError:
        raise ValueError(f"{what} is not valid base64") from None


# -- processes and devices -----------------------------------------------------------------


def run(
    argv: list[str], workdir: str, timeout: float = COMMAND_TIMEOUT_S
) -> Iterator[str]:
    """Run one tool, yielding its output lines and then `exit N`.

    Unbuffered, so mpremote's progress arrives as it happens rather than at exit; killed at
    `timeout`, so a board that stops answering cannot hold the only Pico slot forever.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        argv,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")[: monitor.MAX_LINE_CHARS]
        code = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    yield f"exit {code}"


def _ok(lines: Iterator[str]) -> Iterator[str]:
    """Pass a tool's lines through, raising on its non-zero exit."""
    for line in lines:
        if line.startswith("exit "):
            if line != "exit 0":
                raise _ToolFailed(line)
            return
        yield line


class _ToolFailed(Exception):
    pass


def boot_devices() -> list[dict[str, Any]]:
    return [d for d in ports.scan_boot() if d["pid"] == ports.RP2350_BOOT_PID]


def pico_ttys() -> list[str]:
    return [
        p.device
        for p in ports.scan()
        if p.is_pico and p.pid == ports.PICO_MICROPYTHON_PID
    ]


def wait_for(
    probe: Callable[[], Any],
    seconds: float,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """Poll `probe` until it returns something truthy or the time runs out (then the last
    falsy answer)."""
    nap = sleep or time.sleep
    deadline = time.monotonic() + seconds
    while True:
        found = probe()
        if found or time.monotonic() >= deadline:
            return found
        nap(POLL_S)


def find_tty(serial: str, seconds: float) -> str:
    """The MicroPython tty of the board with `serial`, or of the only Pico if it has none."""

    def probe() -> str:
        ttys = pico_ttys()
        if serial:
            for tty in ttys:
                if ports.usb_serial(tty) == serial:
                    return tty
        return ttys[0] if len(ttys) == 1 else ""

    return wait_for(probe, seconds)


def _open_console(port: str):  # type: ignore[no-untyped-def] - pyserial ships no stubs
    import serial  # pyright: ignore[reportMissingModuleSource]

    # DTR on: MicroPython's USB console only sends to a host that has asserted it, and on a
    # Pico DTR and RTS are no reset lines (unlike the panel's, see monitor.py).
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = monitor.READ_TIMEOUT_S
    ser.dtr = True
    ser.open()
    return ser


def console(port: str, seconds: float) -> Iterator[str]:
    """The board's console lines for up to `seconds`, through monitor's line decoding."""
    ser = _open_console(port)
    deadline = time.monotonic() + seconds

    def chunks() -> Iterator[bytes]:
        while time.monotonic() < deadline:
            data = ser.read(4096)
            if data:
                yield data

    try:
        yield from monitor.decode_lines(chunks())
    finally:
        ser.close()


# -- the operations ------------------------------------------------------------------------


def _exclusive() -> None:
    if not _busy.acquire(blocking=False):
        raise PicoError("internal", "another Pico job is running on this box")


def load(port: str | None, uf2: bytes, sha256: str) -> Iterator[str]:
    """Put MicroPython on a Pico: into its bootloader if it is running, then picotool."""
    _exclusive()
    workdir = tempfile.mkdtemp(prefix="pico-")
    try:
        digest = hashlib.sha256(uf2).hexdigest()
        if digest != sha256:
            raise PicoError("load_failed", f"the UF2 is {digest}, expected {sha256}")
        serial = ""
        if port is not None:
            serial = ports.usb_serial(port)
            yield f"asking {port} to enter its bootloader"
            if not monitor.release(port):
                raise PicoError("usb_gone", f"{port} is held by a monitor")
            # Its exit status is not news: the board drops off USB under mpremote's feet.
            for line in run(bootloader_argv(port), workdir, timeout=30):
                if not line.startswith("exit "):
                    yield line
            time.sleep(SETTLE_S)
        devices = wait_for(lambda: boot_devices(), BOOT_WAIT_S)
        if not devices:
            raise PicoError("not_bootsel", "no RP2350 bootloader appeared on USB")
        if len(devices) > 1:
            raise PicoError(
                "not_bootsel", "two boards are in their bootloader; unplug one"
            )
        dev = devices[0]
        serial = str(dev["serial"] or serial)
        path = os.path.join(workdir, "micropython.uf2")
        with open(path, "wb") as fh:
            fh.write(uf2)
        argv = picotool_argv(int(dev["bus"]), int(dev["address"]), path)
        yield " ".join(argv)
        try:
            yield from _ok(run(argv, workdir))
        except _ToolFailed as exc:
            raise PicoError("load_failed", f"picotool failed ({exc})") from None
        time.sleep(SETTLE_S)
        tty = find_tty(serial, TTY_WAIT_S)
        if not tty:
            raise PicoError("usb_gone", "MicroPython did not come up on USB")
        yield f"OK port={tty}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _busy.release()


def identify(port: str) -> dict[str, str]:
    """What the board says it is: its pairing code, MicroPython version and machine."""
    _exclusive()
    workdir = tempfile.mkdtemp(prefix="pico-")
    try:
        if not monitor.release(port):
            raise PicoError("usb_gone", f"{port} is held by a monitor")
        lines = list(run(identify_argv(port), workdir, timeout=30))
        for line in lines:
            if line.startswith(IDENTIFY_MARK):
                try:
                    found = json.loads(line[len(IDENTIFY_MARK) :])
                except ValueError:
                    break
                if isinstance(found, dict):
                    return {
                        k: str(found.get(k) or "")
                        for k in ("code", "micropython", "machine")
                    }
        tail = "; ".join(lines[-3:])
        raise PicoError("usb_gone", f"the board did not answer ({tail})")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _busy.release()


def _reset_and_find(port: str, workdir: str) -> Iterator[str]:
    """Hard-reset the board (clearing its watchdog) and yield its tty once it is back."""
    serial = ports.usb_serial(port)
    if not monitor.release(port):
        raise PicoError("usb_gone", f"{port} is held by a monitor")
    yield "restarting the robot"
    for line in run(reset_argv(port), workdir, timeout=30):
        if not line.startswith("exit "):
            yield line
    time.sleep(SETTLE_S)
    tty = find_tty(serial, RESET_WAIT_S)
    if not tty:
        raise PicoError("usb_gone", "the robot did not come back on USB")
    yield f"port={tty}"


def watch_join(port: str, seconds: float) -> Iterator[str]:
    """The robot's console after a reset, until it joins Wi-Fi and dials the box."""
    serial = ports.usb_serial(port)
    time.sleep(SETTLE_S)
    tty = find_tty(serial, RESET_WAIT_S)
    if not tty:
        raise PicoError("usb_gone", "the robot did not come back on USB")
    deadline = time.monotonic() + seconds
    joined_at: float | None = None
    for line in console(tty, seconds):
        yield line
        if line.startswith("WIFI_FAIL"):
            raise PicoError("wifi_failed", "it could not join any of its networks")
        if line.startswith("WIFI_OK"):
            joined_at = time.monotonic()
        if joined_at is not None and line.startswith("handshake:"):
            yield "OK"
            return
        if joined_at is not None and time.monotonic() - joined_at > HANDSHAKE_WAIT_S:
            yield "OK"
            return
        if time.monotonic() > deadline:
            break
    if joined_at is not None:
        yield "OK"
        return
    raise PicoError("timeout", "the robot said nothing about Wi-Fi in time")


def _port_of(line: str) -> str | None:
    return line[len("port=") :] if line.startswith("port=") else None


def provision(
    port: str, files: list[tuple[str, bytes]], secrets: bytes, watch_s: int
) -> Iterator[str]:
    """Copy the firmware and secrets.py, restart, and watch the robot join."""
    _exclusive()
    workdir = tempfile.mkdtemp(prefix="pico-")
    try:
        for line in _reset_and_find(port, workdir):
            port = _port_of(line) or port
            yield line
        pairs: list[tuple[str, str]] = []
        for name, data in files:
            with open(os.path.join(workdir, name), "wb") as fh:
                fh.write(data)
            pairs.append((name, name))
        with open(os.path.join(workdir, "secrets.py"), "wb") as fh:
            fh.write(secrets)
        pairs.append(("secrets.py", "secrets.py"))
        yield f"copying {len(pairs)} files"
        try:
            yield from _ok(run(copy_argv(port, pairs), workdir))
        except _ToolFailed as exc:
            raise PicoError("copy_failed", f"mpremote could not copy ({exc})") from None
        # A marker line the api moves the job's phase on.
        yield "copied"
        # The secrets are on the board now; nothing on this side needs them any longer.
        shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)
        yield "restarting the robot"
        for line in run(reset_argv(port), workdir, timeout=30):
            if not line.startswith("exit "):
                yield line
        yield from watch_join(port, max(1, min(int(watch_s), MAX_WATCH_S)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _busy.release()


def rekey(port: str, key: str, watch_s: int = MAX_WATCH_S) -> Iterator[str]:
    """Write a new device key into the robot's secrets.py, restart, and watch it rejoin."""
    script = rekey_script(key)
    _exclusive()
    workdir = tempfile.mkdtemp(prefix="pico-")
    try:
        for line in _reset_and_find(port, workdir):
            port = _port_of(line) or port
            yield line
        path = os.path.join(workdir, "rekey.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        yield "writing the new key"
        said = False
        try:
            for line in _ok(run(run_argv(port, path), workdir)):
                said = said or line.strip() == REKEY_OK
                if "GBREKEY" not in line:
                    yield line
        except _ToolFailed:
            said = False
        if not said:
            raise PicoError("copy_failed", "secrets.py has no single GROWBOT_KEY line")
        os.remove(path)
        yield "key written"
        yield "restarting the robot"
        for line in run(reset_argv(port), workdir, timeout=30):
            if not line.startswith("exit "):
                yield line
        yield from watch_join(port, max(1, min(int(watch_s), MAX_WATCH_S)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _busy.release()
