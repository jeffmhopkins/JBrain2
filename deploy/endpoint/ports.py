"""Which serial ports the box can see, and which of them is a panel.

Split out from `server.py` because it is the half with no device in it: given a
sysfs tree it is pure string work, so it is testable on a machine with nothing
plugged in — which is every machine this repo's CI runs on.

The ESP32-S3 (and the C6) presents its NATIVE USB (a USB-Serial-JTAG peripheral inside the chip,
not a bridge chip) so it enumerates as a kernel CDC-ACM tty: `/dev/ttyACM*`, NOT the
`/dev/ttyUSB*` a CP210x or CH340 board would give you. That distinction is why this
sidecar needs `/dev` and the tty cgroup rules rather than the `/dev/bus/usb` mapping
the `sdr` sidecar uses for its libusb dongle — an ACM tty is a kernel character
device and is not reachable through that path at all.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

# Espressif's USB vendor id. The built-in USB-Serial-JTAG is 1001; other Espressif USB
# devices exist, so the vendor alone is what marks a port "probably the panel" and the
# product id only sharpens the label. 1001 is the SAME on the S3 and the C6 — it names
# the peripheral, not the chip — so the label says both rather than claim one it cannot
# know. Which board it is comes from the owner's pick, and esptool's `--chip` refuses a
# mismatch before writing anything.
ESPRESSIF_VID = "303a"
USB_SERIAL_JTAG_PID = "1001"

# Raspberry Pi's vendor id: the GrowBot's Pico 2 W. 0005 is MicroPython's own CDC-ACM port;
# 000f is the RP2350's ROM bootloader, a raw USB device with no tty (so it is found under
# /sys/bus/usb/devices, not /dev). 0003 is the RP2040's bootloader, listed so an owner who
# plugs in the wrong Pico is told so rather than shown nothing.
PICO_VID = "2e8a"
PICO_MICROPYTHON_PID = "0005"
RP2350_BOOT_PID = "000f"
RP2040_BOOT_PID = "0003"

# The usual USB-UART bridges. Not panels, but worth naming in the picker so an owner
# looking at three ports can tell which is the Arduino they forgot about.
KNOWN_BRIDGES = {
    "10c4": "Silicon Labs CP210x",
    "1a86": "QinHeng CH340/CH341",
    "0403": "FTDI",
}


@dataclass(frozen=True)
class Port:
    device: str
    vid: str
    pid: str
    product: str
    manufacturer: str
    # Whether this looks like one of the panels. Advisory: the owner still picks, because
    # guessing wrong here would flash something that is not a panel.
    is_espressif: bool
    label: str
    # A Raspberry Pi device. Every Pico route refuses a port without it, so mpremote is never
    # pointed at a panel or a bridge.
    is_pico: bool = False


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _usb_node(sysfs: str, name: str) -> str | None:
    node = os.path.join(sysfs, "class", "tty", name, "device")
    for _ in range(6):
        if _read(os.path.join(node, "idVendor")):
            return node
        parent = os.path.join(node, "..")
        if not os.path.isdir(parent):
            break
        node = parent
    return None


def usb_serial(device: str, sysfs: str = "/sys") -> str:
    """The USB serial string of the device behind a tty (`/dev/ttyACM0`), or "".

    MicroPython on a Pico reports the chip's unique id here, which is how a board is found
    again after a reset hands it a different ttyACM number.
    """
    node = _usb_node(sysfs, os.path.basename(device))
    return _read(os.path.join(node, "serial")) if node else ""


def _usb_attrs(sysfs: str, name: str) -> tuple[str, str, str, str]:
    """Walk up from the tty to the USB device that owns it.

    `/sys/class/tty/ttyACM0/device` is the USB *interface*; idVendor/idProduct live on
    its parent, the device. Walking rather than hardcoding `../..` keeps this working
    across the kernel's occasional reshuffles of that tree, and the loop is bounded so a
    symlink cycle cannot hang the port list.
    """
    node = _usb_node(sysfs, name)
    if node is None:
        return ("", "", "", "")
    return (
        _read(os.path.join(node, "idVendor")).lower(),
        _read(os.path.join(node, "idProduct")).lower(),
        _read(os.path.join(node, "product")),
        _read(os.path.join(node, "manufacturer")),
    )


def _label(vid: str, pid: str, product: str, manufacturer: str) -> str:
    if vid == ESPRESSIF_VID:
        if pid == USB_SERIAL_JTAG_PID:
            return "Espressif USB-Serial/JTAG (ESP32-S3 or ESP32-C6)"
        return f"Espressif device {pid or '????'}"
    if vid == PICO_VID:
        if pid == PICO_MICROPYTHON_PID:
            return "Raspberry Pi Pico running MicroPython"
        return f"Raspberry Pi device {pid or '????'}"
    bridge = KNOWN_BRIDGES.get(vid)
    if bridge:
        return f"{bridge} serial adapter"
    named = " ".join(p for p in (manufacturer, product) if p)
    return named or "unknown serial device"


def scan(sysfs: str = "/sys", dev: str = "/dev") -> list[Port]:
    """Every serial port the container can see, Espressif ones first.

    An empty list is the answer to a real question — "can this container see the panel
    at all" — so it is never an error here. The api turns it into a sentence for the
    owner rather than a failure, because with no terminal on the box this list IS the
    diagnostic.
    """
    try:
        names = sorted(
            n
            for n in os.listdir(dev)
            if n.startswith("ttyACM") or n.startswith("ttyUSB")
        )
    except OSError:
        return []

    ports: list[Port] = []
    for name in names:
        vid, pid, product, manufacturer = _usb_attrs(sysfs, name)
        ports.append(
            Port(
                device=os.path.join(dev, name),
                vid=vid,
                pid=pid,
                product=product,
                manufacturer=manufacturer,
                is_espressif=vid == ESPRESSIF_VID,
                label=_label(vid, pid, product, manufacturer),
                is_pico=vid == PICO_VID,
            )
        )
    # Espressif first so the panel is the default pick when several ports exist;
    # stable by device name within each group so the list does not shuffle between polls.
    ports.sort(key=lambda p: (not p.is_espressif, p.device))
    return ports


def as_json(ports: list[Port]) -> list[dict[str, object]]:
    return [asdict(p) for p in ports]


def _int(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def scan_boot(sysfs: str = "/sys") -> list[dict[str, object]]:
    """Raspberry Pi bootloaders on USB: what picotool can write to.

    A blank RP2350 is one of these from the moment it is plugged in, so the first flash needs
    no button. Only USB *device* nodes are read (`1-2`, never the interface `1-2:1.0`), and
    the bus and address come from the kernel's own `busnum`/`devnum`, which are the only
    values picotool is ever handed.
    """
    root = os.path.join(sysfs, "bus", "usb", "devices")
    try:
        names = sorted(n for n in os.listdir(root) if ":" not in n)
    except OSError:
        return []
    found: list[dict[str, object]] = []
    for name in names:
        node = os.path.join(root, name)
        if _read(os.path.join(node, "idVendor")).lower() != PICO_VID:
            continue
        pid = _read(os.path.join(node, "idProduct")).lower()
        if pid not in (RP2350_BOOT_PID, RP2040_BOOT_PID):
            continue
        bus = _int(_read(os.path.join(node, "busnum")))
        address = _int(_read(os.path.join(node, "devnum")))
        if bus is None or address is None:
            continue
        label = (
            "RP2350 bootloader (a Pico 2 W ready for MicroPython)"
            if pid == RP2350_BOOT_PID
            else "RP2040 bootloader (not supported: GrowBot needs a Pico 2 W)"
        )
        found.append(
            {
                "bus": bus,
                "address": address,
                "pid": pid,
                "serial": _read(os.path.join(node, "serial")),
                "label": label,
            }
        )
    return found
