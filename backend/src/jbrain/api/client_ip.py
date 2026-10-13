"""Who is actually on the other end of a request — through Caddy, and through the tunnel.

Every request a browser or a panel makes arrives from Caddy, so `request.client.host` is the
proxy container's address for all of them: every rate limiter keyed on it was one global
bucket, and `nudge` read the first `X-Forwarded-For` hop, which a client can write itself.

What can be trusted, and why:

- **`X-Real-IP`, only when the TCP peer is the proxy.** Caddy sets it from its own peer
  (`header_up X-Real-IP {remote_host}` in `deploy/Caddyfile`), overwriting whatever the client
  sent. Anything else that reaches the api directly — a jcode or pysandbox container on its
  own network — can forge the header, so for any other peer the socket address is the answer.
- **`Cf-Connecting-IP`, only when `X-Real-IP` is the `cloudflared` container.** In tunnel mode
  Caddy's peer is the connector, and Cloudflare's edge sets the header. In direct mode a LAN
  client reaching Caddy's published port can send the same header, so it is read only when the
  hop before Caddy is provably the connector — by its resolved address, not by "a private
  address" or the bridge range, which the docker gateway that published-port and hairpin
  connections arrive from also matches.

The two service names are resolved off the request path (`refresh`, from a lifespan task) and
read from a cache here, so no request ever waits on DNS. A name that does not resolve — the
`cloudflared` service off its profile, or no compose network at all in a test — trusts nothing,
which degrades to the socket address: the old single bucket, never a spoofable one.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Iterator

import structlog
from starlette.requests import HTTPConnection

log = structlog.get_logger(__name__)

PROXY_SERVICE = "proxy"
TUNNEL_SERVICE = "cloudflared"

#: How often the addresses are re-resolved. A recreated container can come back on a new
#: address; until the next refresh its traffic keys on the socket address, which is safe.
REFRESH_S = 60.0

_resolved: dict[str, frozenset[str]] = {}
_pinned: dict[str, frozenset[str]] | None = None


def _norm(value: str) -> str | None:
    """A canonical address string, or None for anything that is not an IP address.

    IPv4-mapped IPv6 (`::ffff:a.b.c.d`) folds to the IPv4 form so a dual-stack socket and the
    resolver agree."""
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _addrs(name: str) -> frozenset[str]:
    if _pinned is not None:
        return _pinned.get(name, frozenset())
    return _resolved.get(name, frozenset())


async def _resolve(name: str) -> frozenset[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return frozenset()
    found = {_norm(str(info[4][0])) for info in infos}
    return frozenset(a for a in found if a)


async def refresh() -> None:
    """Re-resolve the proxy and tunnel services. Never raises: a failure trusts nothing."""
    for name in (PROXY_SERVICE, TUNNEL_SERVICE):
        addrs = await _resolve(name)
        if addrs != _resolved.get(name):
            log.info("client_ip.resolved", service=name, addrs=sorted(addrs))
        _resolved[name] = addrs


async def refresh_forever() -> None:
    """The lifespan task: resolve now, then keep the cache current."""
    while True:
        await refresh()
        await asyncio.sleep(REFRESH_S)


@contextlib.contextmanager
def pinned(*, proxy: tuple[str, ...] = (), tunnel: tuple[str, ...] = ()) -> Iterator[None]:
    """Fix the service addresses for a test, ignoring whatever the lifespan resolves."""
    global _pinned
    prev = _pinned
    _pinned = {PROXY_SERVICE: frozenset(proxy), TUNNEL_SERVICE: frozenset(tunnel)}
    try:
        yield
    finally:
        _pinned = prev


def client_ip(conn: HTTPConnection) -> str | None:
    """The originating client's address for a request or a WebSocket (see the module doc)."""
    if conn.client is None:
        return None
    peer = conn.client.host
    if _norm(peer) not in _addrs(PROXY_SERVICE):
        return peer
    real = _norm(conn.headers.get("x-real-ip", ""))
    if real is None:
        return peer
    if real in _addrs(TUNNEL_SERVICE):
        return _norm(conn.headers.get("cf-connecting-ip", "")) or real
    return real
