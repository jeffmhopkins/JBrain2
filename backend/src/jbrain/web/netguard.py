"""The public-address rule and the pinned TCP layer every direct outbound fetch connects through.

`guard_public_host` checks a URL's host before the request, but a check that resolves and a
connect that resolves AGAIN is a DNS-rebinding window: a name that answers public to the
check and `127.0.0.1` to the connect walks past it (SECURITY_REVIEW_FIXES_PLAN.md F9). So
the direct-fetch clients do not connect by name. Their transport's network backend resolves
the host itself, refuses the connection if ANY answer is non-public, and dials the validated
address. TLS still uses the hostname (httpcore takes SNI from the request origin, not from
the address dialled) and so does the Host header, so certificates and virtual hosts behave
exactly as before. Every redirect hop and every pooled connection is a fresh `connect_tcp`,
so the rule covers them all.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable

import anyio
import httpcore
import httpx

# httpcore's top-level name is a stub when anyio is absent, which hides its type; anyio is
# always installed here (httpx depends on it), so import the real class.
from httpcore._backends.anyio import AnyIOBackend

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# `is_global` misses these on some Python versions or by design: NAT64's well-known prefix
# and the deprecated IPv4-compatible block (`::a9fe:a9fe` is 169.254.169.254) both carry a v4
# address the v4 rules never see; site-local is deprecated but still routed on some LANs; 6to4
# and Teredo embed an IPv4 address too. Squid's deny list carries the same ranges.
_DENIED_V6 = tuple(
    ipaddress.IPv6Network(n)
    for n in ("64:ff9b::/96", "::/96", "fec0::/10", "2002::/16", "2001::/32")
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class NonPublicAddressError(httpx.ConnectError):
    """A connection refused because the host resolved to a non-public address. An
    `httpx.ConnectError`, so every caller's existing `httpx.HTTPError` handling covers it."""


def is_public_ip(ip: IPAddress) -> bool:
    """Whether an address is a routable public one. `is_global` rather than `not is_private`:
    it also excludes CGNAT 100.64/10, which Python counts as neither. An IPv4-mapped address
    is judged by the v4 address inside it."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in net for net in _DENIED_V6):
        return False
    return ip.is_global and not ip.is_multicast


async def system_resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, in resolver order, off the event loop."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _all_public(addresses: Iterable[str]) -> bool:
    try:
        return all(is_public_ip(ipaddress.ip_address(a.split("%", 1)[0])) for a in addresses)
    except ValueError:
        return False


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolve, validate, then dial the validated address. Refuses the whole name when any
    answer is non-public (a mixed answer is exactly what a rebinding attack returns), and
    refuses Unix sockets outright. `resolver` and `inner` are the test seams: a fake DNS and a
    fake socket layer, so a rebinding can be staged with no network."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolve = resolver or system_resolve
        self._inner: httpcore.AsyncNetworkBackend = inner or AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # One deadline for the whole connect: resolving and every address tried share the
        # caller's budget, so a slow resolver or a list of dead addresses cannot multiply it.
        deadline = None if timeout is None else anyio.current_time() + timeout
        try:
            with anyio.fail_after(timeout):
                addresses = await self._resolve(host, port)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"resolving {host} timed out") from exc
        except OSError as exc:
            raise httpcore.ConnectError(f"could not resolve {host}") from exc
        if not addresses or not _all_public(addresses):
            raise NonPublicAddressError("that URL points at a non-public address")
        last: Exception | None = None
        for address in addresses:
            remaining = None if deadline is None else deadline - anyio.current_time()
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout(f"connecting to {host} timed out")
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise NonPublicAddressError("a Unix socket is never a public host")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx's own transport with its connection pool rebuilt over `PinnedBackend` (httpx
    offers no network-backend knob). Proxies are deliberately not supported: a proxy would
    be dialled through the pin and refused as the private address it is."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        context = ssl_context or httpx.create_ssl_context()
        super().__init__(verify=context)
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        # `_pool` is httpx-private (hence the `httpx<0.29` pin); test_netguard's end-to-end
        # fetches fail loudly if a release renames it.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=PinnedBackend(resolver=resolver, inner=inner),
        )


def pinned_transport(
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    inner: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncBaseTransport:
    """The transport a direct fetch should use: an injected one as-is (a test's mock, which
    has no network to pin), otherwise a fresh pinned transport. Fresh per client because an
    `httpx.AsyncClient` closes its transport on exit."""
    if transport is not None:
        return transport
    return PinnedTransport(inner=inner)
