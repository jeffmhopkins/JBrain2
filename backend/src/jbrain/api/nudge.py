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

import asyncio
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


#: device_id -> the live event streams that device currently holds. A set rather than a single
#: entry because a reconnecting panel can briefly hold two: the old connection has not been
#: reaped yet when the new one registers, and waking only one of them would be a coin flip over
#: which. Both get set; the dead one is discarded when its generator unwinds.
_streams: dict[str, set[asyncio.Queue[str]]] = {}


def attach(device_id: str) -> asyncio.Queue[str]:
    """Register a held-open event stream for a device, and hand back its mailbox."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
    _streams.setdefault(device_id, set()).add(q)
    log.info("nudge.stream_open", device=device_id, streams=len(_streams[device_id]))
    return q


def detach(device_id: str, q: asyncio.Queue[str]) -> None:
    """Forget a stream that has gone away. Idempotent — a generator can unwind more than once."""
    live = _streams.get(device_id)
    if live is None:
        return
    live.discard(q)
    if not live:
        _streams.pop(device_id, None)
    log.info("nudge.stream_closed", device=device_id, streams=len(live))


def is_connected(device_id: str) -> bool:
    """Whether this device is holding a stream right now — for the operator, who otherwise
    cannot tell a panel that is listening from one that is merely reachable."""
    return bool(_streams.get(device_id))


def fire(device_id: str, why: str) -> bool:
    """Tell one device to poll now. Returns whether anything was actually delivered.

    TWO CHANNELS, AND THE DATAGRAM IS THE FALLBACK. A held-open stream is the fast path and
    needs no address at all — the panel came to us, so there is nothing to look up and nothing
    to go stale. The datagram covers the window where the stream is down: a panel reconnecting,
    a panel whose Wi-Fi just came back, a box that restarted and lost its stream registry.
    Both are fired rather than one-or-the-other, because the cost of a redundant nudge is a
    poll that finds nothing and the cost of a missed one is a child's message sitting unheard.
    """
    woke = False
    for q in list(_streams.get(device_id, ())):
        try:
            q.put_nowait(why)
            woke = True
        except asyncio.QueueFull:
            # The panel is not draining. Not an error and not worth growing a buffer for: every
            # item in that queue means the same thing ("come and ask"), so a full queue has
            # already delivered the message this one carries.
            woke = True
    return _fire_datagram(device_id, why) or woke


def _fire_datagram(device_id: str, why: str) -> bool:
    """The UDP half. Kept separate so the stream path is testable without a socket."""
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
    """Tell every device we can reach. For settings and commands not aimed at one panel.

    The union of both channels, not just the address book: a panel that has never been
    remembered (the box restarted) but is holding a stream is still perfectly reachable, and
    counting only `_seen` would report it as unreachable while talking to it."""
    targets = set(_seen) | set(_streams)
    return sum(1 for device_id in sorted(targets) if fire(device_id, why))
