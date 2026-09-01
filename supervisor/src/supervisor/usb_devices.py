"""Host USB inventory, read from sysfs.

Like `host_metrics`, this reflects the HOST rather than the container: Docker
mounts the host's /sys read-only into every container, and USB is not namespaced,
so `/sys/bus/usb/devices/` lists every device plugged into the box. Crucially this
needs **no device passthrough** — enumerating and naming a device is a sysfs read;
only *using* one needs `/dev/bus/usb`. That is what lets the debug console answer
"is the dongle actually there, and what is it called?" before any of the SDR stack
exists. Paths are injectable for tests.

`/sys/bus/usb/devices/` mixes three kinds of entry, distinguished by name:

    usb1        a root hub          — has idVendor, reported like any device
    1-2         a device            — has idVendor/idProduct/manufacturer/product
    1-2:1.0     an interface        — no idVendor; carries the bound `driver` symlink

We report the devices and fold each one's interface drivers back onto it, because
the question that matters for an SDR is *which kernel driver claimed it* — a
DVB-T driver holding the dongle is exactly what stops userspace from opening it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# USB ids that are RTL2832U-family SDR dongles. The value is the name to show;
# the device's own `product` string is preferred when it has one, so this table
# only has to be right about *whether* a device is an SDR, not what to call it.
KNOWN_SDR_IDS: dict[str, str] = {
    "0bda:2832": "Realtek RTL2832U (RTL-SDR)",
    "0bda:2838": "Realtek RTL2838UHIDIR (RTL-SDR)",
    "0ccd:00a9": "TerraTec Cinergy T Stick Black (RTL2832U)",
    "0ccd:00b3": "TerraTec NOXON DAB/DAB+ (RTL2832U)",
    "1554:5020": "PixelView PV-DT235U (RTL2832U)",
    "15f4:0131": "Astrometa DVB-T (RTL2832U)",
    "185b:0620": "Compro VideoMate U620F (RTL2832U)",
    "1b80:d3a4": "Gigabyte GT-U7300 (RTL2832U)",
    "1d19:1101": "Dexatek DK DVB-T (RTL2832U)",
}


@dataclass(frozen=True, slots=True)
class UsbDevice:
    """One USB device as sysfs describes it.

    `drivers` are the kernel drivers bound to this device's interfaces — empty
    when nothing has claimed it, which for an SDR is the state we want. `bus`/
    `device` give the `/dev/bus/usb/BBB/DDD` node an sdr container would need
    passed through; they are None only on a malformed sysfs entry."""

    name: str  # the sysfs entry, e.g. "1-2"
    vendor_id: str
    product_id: str
    manufacturer: str | None
    product: str | None
    serial: str | None
    bus: int | None
    device: int | None
    drivers: tuple[str, ...]

    @property
    def usb_id(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"

    @property
    def is_sdr(self) -> bool:
        return self.usb_id in KNOWN_SDR_IDS

    @property
    def sdr_name(self) -> str | None:
        """What to call it: the device's own product string when it has one,
        else our table's name. None when it isn't an SDR at all."""
        if not self.is_sdr:
            return None
        return self.product or KNOWN_SDR_IDS[self.usb_id]

    @property
    def device_node(self) -> str | None:
        if self.bus is None or self.device is None:
            return None
        return f"/dev/bus/usb/{self.bus:03d}/{self.device:03d}"


def _read(path: Path) -> str | None:
    """One sysfs attribute, stripped. None when absent or unreadable — a device
    can vanish mid-walk (unplugged), and a partial read must never sink the scan."""
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def _int(path: Path) -> int | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _drivers_for(root: Path, name: str) -> tuple[str, ...]:
    """The kernel drivers bound to `name`'s interfaces.

    Interfaces are siblings in this directory named `<device>:<config>.<iface>`,
    each with a `driver` symlink into bus/usb/drivers/. We take the link's target
    name rather than following it, so a fake tree in tests needs no real driver."""
    found: list[str] = []
    prefix = f"{name}:"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        link = entry / "driver"
        try:
            found.append(Path(link.readlink()).name)
        except OSError:
            continue
    # A device with several interfaces on one driver reports it once.
    return tuple(dict.fromkeys(found))


def read_usb_devices(root: Path = Path("/sys/bus/usb/devices")) -> list[UsbDevice]:
    """Every USB device on the host, root hubs included, sorted by sysfs name.

    Entries without an `idVendor` are interfaces, not devices, and are folded into
    their parent's `drivers` instead of being reported. Returns [] when sysfs is
    absent — a container without /sys, or a non-Linux host — rather than raising,
    so the caller can say "cannot tell" instead of failing."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []

    devices: list[UsbDevice] = []
    for entry in entries:
        vendor = _read(entry / "idVendor")
        product_id = _read(entry / "idProduct")
        if vendor is None or product_id is None:
            continue  # an interface, or a device that disappeared mid-walk
        devices.append(
            UsbDevice(
                name=entry.name,
                vendor_id=vendor.lower(),
                product_id=product_id.lower(),
                manufacturer=_read(entry / "manufacturer"),
                product=_read(entry / "product"),
                serial=_read(entry / "serial"),
                bus=_int(entry / "busnum"),
                device=_int(entry / "devnum"),
                drivers=_drivers_for(root, entry.name),
            )
        )
    return devices


def find_sdrs(devices: list[UsbDevice]) -> list[UsbDevice]:
    """The RTL-SDR-family devices among a scan, in scan order."""
    return [d for d in devices if d.is_sdr]


@dataclass(frozen=True, slots=True)
class UsbScan:
    """One sweep of the bus, carrying whether we could look at all.

    Readability and the device list travel together because apart they lie: an
    empty list means "no devices" only if sysfs was readable, and otherwise means
    "we could not see" — which is a different fault with a different fix."""

    sysfs_readable: bool
    devices: list[UsbDevice]

    @property
    def sdrs(self) -> list[UsbDevice]:
        return find_sdrs(self.devices)


def scan(root: Path = Path("/sys/bus/usb/devices")) -> UsbScan:
    """Read the bus and say whether we could."""
    return UsbScan(sysfs_readable=root.is_dir(), devices=read_usb_devices(root))
