#!/usr/bin/env python3
"""Publish mDNS CNAME aliases pointing at this host's own .local name.

The LAN site (docs/runbooks/LOCAL_ACCESS.md) answers to a fixed name like jbrain.local,
but avahi only auto-advertises the box's *system* hostname. Rather than rename
the host, we publish jbrain.local as a CNAME -> <hostname>.local. avahi keeps a
published record only while the D-Bus client that registered it stays connected,
so this runs as a long-lived service (deploy/jbrain-avahi-alias.service).

avahi's D-Bus constants are inlined so the only runtime deps are python3-dbus and
python3-gi (no python3-avahi). The dbus/gi imports live inside main() so the
pure wire-format encoder can be imported and tested without them.
"""

import sys


def encode_rdata(fqdn: str) -> bytes:
    """DNS wire format for a CNAME target: length-prefixed labels, NUL-terminated."""
    out = bytearray()
    for label in fqdn.split("."):
        if label:
            out.append(len(label))
            out.extend(label.encode("ascii"))
    out.append(0)
    return bytes(out)


def main(aliases: list[str]) -> None:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    # avahi D-Bus constants (avahi-common/defs.h) — inlined to avoid python3-avahi.
    DBUS_NAME = "org.freedesktop.Avahi"
    IF_UNSPEC, PROTO_UNSPEC = -1, -1
    CLASS_IN, TYPE_CNAME, TTL = 0x01, 0x05, 60

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    server = dbus.Interface(
        bus.get_object(DBUS_NAME, "/"), "org.freedesktop.Avahi.Server"
    )
    group = dbus.Interface(
        bus.get_object(DBUS_NAME, server.EntryGroupNew()),
        "org.freedesktop.Avahi.EntryGroup",
    )
    rdata = encode_rdata(str(server.GetHostNameFqdn()))
    for alias in aliases:
        group.AddRecord(
            IF_UNSPEC, PROTO_UNSPEC, dbus.UInt32(0),
            alias, CLASS_IN, TYPE_CNAME, TTL, rdata,
        )
    group.Commit()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main(sys.argv[1:] or ["jbrain.local"])
