"""Which serial ports the box can see, and which of them is a panel.

Split out from `server.py` because it is the half with no device in it: given a
sysfs tree it is pure string work, so it is testable on a machine with nothing
plugged in — which is every machine this repo's CI runs on.

The ESP32-S3 presents its NATIVE USB (a USB-Serial-JTAG peripheral inside the chip,
not a bridge chip) so it enumerates as a kernel CDC-ACM tty: `/dev/ttyACM*`, NOT the
`/dev/ttyUSB*` a CP210x or CH340 board would give you. That distinction is why this
sidecar needs `/dev` and the tty cgroup rules rather than the `/dev/bus/usb` mapping
the `sdr` sidecar uses for its libusb dongle — an ACM tty is a kernel character
device and is not reachable through that path at all.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

# Espressif's USB vendor id. The S3's built-in USB-Serial-JTAG is 1001; other Espressif
# USB devices exist, so the vendor alone is what marks a port "probably the panel" and
# the product id only sharpens the label.
ESPRESSIF_VID = "303a"
S3_USB_SERIAL_JTAG_PID = "1001"

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


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _usb_attrs(sysfs: str, name: str) -> tuple[str, str, str, str]:
    """Walk up from the tty to the USB device that owns it.

    `/sys/class/tty/ttyACM0/device` is the USB *interface*; idVendor/idProduct live on
    its parent, the device. Walking rather than hardcoding `../..` keeps this working
    across the kernel's occasional reshuffles of that tree, and the loop is bounded so a
    symlink cycle cannot hang the port list.
    """
    node = os.path.join(sysfs, "class", "tty", name, "device")
    for _ in range(6):
        vid = _read(os.path.join(node, "idVendor"))
        if vid:
            return (
                vid.lower(),
                _read(os.path.join(node, "idProduct")).lower(),
                _read(os.path.join(node, "product")),
                _read(os.path.join(node, "manufacturer")),
            )
        parent = os.path.join(node, "..")
        if not os.path.isdir(parent):
            break
        node = parent
    return ("", "", "", "")


def _label(vid: str, pid: str, product: str, manufacturer: str) -> str:
    if vid == ESPRESSIF_VID:
        if pid == S3_USB_SERIAL_JTAG_PID:
            return "Espressif ESP32-S3 (native USB)"
        return f"Espressif device {pid or '????'}"
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
            )
        )
    # Espressif first so the panel is the default pick when several ports exist;
    # stable by device name within each group so the list does not shuffle between polls.
    ports.sort(key=lambda p: (not p.is_espressif, p.device))
    return ports


def as_json(ports: list[Port]) -> list[dict[str, object]]:
    return [asdict(p) for p in ports]
