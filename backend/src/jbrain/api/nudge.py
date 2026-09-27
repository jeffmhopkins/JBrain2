"""Telling a panel to come and ask, instead of waiting for it to wonder.

A contentless UDP datagram on the LAN. The panel answers it by doing exactly the
authenticated HTTPS poll it would have done anyway — just now instead of in a minute.

This docstring used to say a real push socket was unaffordable, because a second concurrent
mbedTLS session would not fit in the 31744-byte largest free internal block the panels report.
That was wrong on every step, and ``firmware/main/nudge.h`` carries the corrected reasoning:
31744 is an untouched reserve floor rather than a ceiling, the largest contiguous demand is
~16.6 KB rather than 32 KB, and the panels already sustain two concurrent sessions several
times a day — because this very mechanism wakes two tasks at once. The datagram is a good
choice here; it was never the only one available.

**It carries no data and no authority, and that is the design.** The datagram says only
"something changed"; every actual fact still arrives over the authenticated, TLS-protected
poll that follows. The worst an attacker with a foothold on the LAN can do is make a panel ask
the box a question it is already entitled to ask. Nothing here may ever grow a payload the
panel acts on — the moment it does, this becomes an unauthenticated control channel into a
child's bedroom.

**Best-effort, always.** Every failure is swallowed: a nudge that does not arrive costs
latency, and the poll is still there underneath it. A nudge that raised would cost a child's
message, which is the wrong way round. That is also why the panel keeps a slow poll at all —
UDP may be dropped, and the owner's *"get rid of polling altogether"* is met in the sense that
matters (a message lands in tens of milliseconds) rather than the literal one.
"""

from __future__ import annotations

import socket
import time

import structlog
from fastapi import Request

log = structlog.get_logger(__name__)

#: Spelled in ``firmware/main/nudge.h`` too, and a test pins that the two agree — the same
#: class of contract as the integrity header, which has already been got wrong twice by being
#: written down twice.
NUDGE_PORT = 8267
NUDGE_MAGIC = b"JBN1"

#: How long a remembered address is worth trying. A panel that has not been heard from in an
#: hour has very likely been unplugged, moved, or given a new lease, and firing at a stale
#: address is how one home's traffic ends up at another device on the same subnet.
_STALE_S = 3600.0

#: device_id -> (address, when it was last seen). In memory on purpose: it is a cache of
#: something the panels re-assert every few seconds, so a restart costs one poll interval of
#: latency and nothing else. Persisting it would mean writing a device's network location to
#: disk for no gain.
_seen: dict[str, tuple[str, float]] = {}


def _client_ip(request: Request) -> str | None:
    """The panel's own address, not the proxy's.

    Panels reach the api through Caddy, so ``request.client.host`` is a container address on
    the compose network — firing a nudge at that would send it to the reverse proxy, which has
    no panel behind it. Caddy sets ``X-Forwarded-For``; its FIRST entry is the originating
    client. Falls back to the socket address, which is right when something talks to the api
    directly.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


def remember(device_id: str, request: Request) -> None:
    """Note where a device just called from. Cheap enough to call on every panel request."""
    ip = _client_ip(request)
    if not ip or not device_id:
        return
    prev = _seen.get(device_id)
    _seen[device_id] = (ip, time.monotonic())
    if prev is None or prev[0] != ip:
        # Only on a CHANGE, or this logs once per poll per panel forever. The address moving is
        # worth a line — a panel that starts missing nudges after a router reboot is otherwise
        # a silent latency regression with no fingerprint.
        log.info("nudge.address", device=device_id, addr=ip, was=prev[0] if prev else None)


def fire(device_id: str, why: str) -> bool:
    """Tell one device to poll now. Returns whether a datagram was actually sent."""
    entry = _seen.get(device_id)
    if entry is None:
        return False
    ip, seen_at = entry
    if time.monotonic() - seen_at > _STALE_S:
        return False
    try:
        # A fresh socket per nudge rather than one held open: this fires a handful of times an
        # hour, and a long-lived socket is a thing to own, reopen after a network change, and
        # get wrong. SOCK_DGRAM sendto does not block on a LAN and never waits for a peer.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.25)
            s.sendto(NUDGE_MAGIC, (ip, NUDGE_PORT))
    except OSError as exc:
        # Swallowed by design — see the module docstring. Logged because a push path that
        # silently stopped working looks exactly like a quiet house.
        log.warning("nudge.failed", device=device_id, addr=ip, why=why, error=str(exc))
        return False
    log.info("nudge.sent", device=device_id, addr=ip, why=why)
    return True


def fire_all(why: str) -> int:
    """Tell every device we have an address for. For settings and commands that are not aimed
    at one panel in particular."""
    return sum(1 for device_id in list(_seen) if fire(device_id, why))
