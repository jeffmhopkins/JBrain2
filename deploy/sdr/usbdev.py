"""Resetting a USB device without a terminal and without unplugging it.

`USBDEVFS_RESET` is a PORT reset: the kernel re-enumerates the device exactly as if it
had been unplugged and put back. It is the only recovery for a dongle that is still on
the bus but has stopped answering descriptor reads — the state an RTL-SDR can be left in
by an unclean teardown or a brown-out — and nothing else in this system can clear it. A
container restart does not touch USB; neither does a rebuild.

**Here rather than in the supervisor** because this is the container that has
`/dev/bus/usb` (compose maps it in for the radio tools) and runs as root for it. The
supervisor is the one that can READ `/sys`, which is why the caller supplies the node:
resolving a serial to a node is exactly what a broken device cannot help with, and
sysfs answers it anyway from what the kernel cached at enumeration.

Stdlib only: an ioctl number and `fcntl`. `Dockerfile.sdr`'s rule is apt-only-no-pip
rather than stdlib-only since the spectrum path arrived, but a port reset wants nothing
from either — it is a syscall.

Named `usbdev` rather than `usb` because the sidecar's modules import each other by
bare name off one WORKDIR, and `usb` is what pyusb installs as: nothing here pulls it
in today, and a module that would silently shadow it if anything ever did is a trap
laid for a future reader rather than a name saved.
"""

from __future__ import annotations

import fcntl
import os
import re

#: `_IO('U', 20)` from `<linux/usbdevice_fs.h>`: direction 0, type 'U' (0x55), number 20.
USBDEVFS_RESET = 0x5514

#: The ONLY shape of path this will open. The node arrives over HTTP, and `os.open` on
#: an arbitrary caller-supplied path inside a root container is not a thing to leave
#: open — even one reachable only by the api, because "reachable only by" is a property
#: of today's routing rather than of this function.
_NODE = re.compile(r"^/dev/bus/usb/\d{3}/\d{3}$")


def reset(node: str) -> None:
    """Re-enumerate the device at `node`. Raises OSError if the kernel refuses.

    The node number CHANGES as a result — a reset device comes back as the next free
    address on its bus — so a caller holding one is holding a stale path the moment this
    returns. Re-read the scan rather than reusing it."""
    if not _NODE.match(node):
        raise ValueError(f"{node!r} is not a USB device node")
    handle = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(handle, USBDEVFS_RESET, 0)
    finally:
        os.close(handle)
