"""The public-address rule and the pinned connect under every direct fetch (S2.3, F9).

A check that resolves a name and a connect that resolves it again is a DNS-rebinding
window. These tests stage the rebinding with a fake resolver and a fake socket layer —
no network — and assert the connect is refused, or lands on the address that was validated
while TLS and the Host header keep the name.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import pytest

from jbrain.agent import browse_policy
from jbrain.captions import CaptionTrack, fetch_caption_transcript
from jbrain.web import fetch as fetch_mod
from jbrain.web import netguard
from jbrain.web.favicon import FaviconFetcher
from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.netguard import NonPublicAddressError, PinnedBackend, is_public_ip

# --- The rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1",  # CGNAT: neither private nor global to Python, so `not is_private` passed it
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.17.0.1",
        "192.168.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",  # NAT64 spelling of the metadata address
        "::a9fe:a9fe",  # IPv4-compatible spelling of it
        "fec0::1",  # site-local
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2002:a9fe:a9fe::1",  # 6to4 around 169.254.169.254
        "2001::1",  # Teredo
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_pass(address: str) -> None:
    assert is_public_ip(ipaddress.ip_address(address))


def test_one_rule_serves_every_outbound_path() -> None:
    assert fetch_mod._is_public is is_public_ip
    assert browse_policy._is_public_ip is is_public_ip


# --- A fake socket layer ----------------------------------------------------------------------


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self, response: bytes) -> None:
        self._response = response
        self.written = b""
        self.sni: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        chunk, self._response = self._response[:max_bytes], self._response[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self.written += buffer

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self.sni = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class _Net(httpcore.AsyncNetworkBackend):
    """Answers each dialled ADDRESS with a canned HTTP response; records every dial."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.dialled: list[tuple[str, int]] = []
        self.streams: list[_Stream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dialled.append((host, port))
        if host not in self.responses:
            raise httpcore.ConnectError(f"nothing at {host}")
        stream = _Stream(self.responses[host])
        self.streams.append(stream)
        return stream

    async def sleep(self, seconds: float) -> None:
        return None


_PUBLIC = "93.184.216.34"
_BODY = b"<html><head><title>T</title></head><body><p>hello there</p></body></html>"
_PAGE = (
    b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\nconnection: close\r\n"
    + f"content-length: {len(_BODY)}\r\n\r\n".encode()
    + _BODY
)


def _dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]]) -> list[str]:
    """Patch the system resolver: each name answers its list in turn (the last repeats), so
    a rebinding is `["<public>", "127.0.0.1"]`. Returns the names asked, in order."""
    asked: list[str] = []

    def getaddrinfo(host: str, port: Any, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
        asked.append(host)
        seq = answers[host]
        ip = seq.pop(0) if len(seq) > 1 else seq[0]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


# --- The pinned backend -----------------------------------------------------------------------


async def test_the_connect_dials_the_validated_address_not_the_name() -> None:
    net = _Net({_PUBLIC: b""})

    async def resolve(host: str, port: int) -> list[str]:
        return [_PUBLIC]

    await PinnedBackend(resolver=resolve, inner=net).connect_tcp("site.example", 443)
    assert net.dialled == [(_PUBLIC, 443)]


async def test_a_mixed_answer_refuses_the_whole_name() -> None:
    net = _Net({_PUBLIC: b""})

    async def resolve(host: str, port: int) -> list[str]:
        return [_PUBLIC, "127.0.0.1"]

    with pytest.raises(NonPublicAddressError):
        await PinnedBackend(resolver=resolve, inner=net).connect_tcp("rebind.example", 80)
    assert net.dialled == []


async def test_an_unresolvable_name_is_a_connect_error() -> None:
    async def resolve(host: str, port: int) -> list[str]:
        raise socket.gaierror("no such name")

    with pytest.raises(httpcore.ConnectError):
        await PinnedBackend(resolver=resolve, inner=_Net({})).connect_tcp("nope.example", 80)


async def test_a_dead_address_falls_through_to_the_next_validated_one() -> None:
    net = _Net({"8.8.8.8": b""})

    async def resolve(host: str, port: int) -> list[str]:
        return [_PUBLIC, "8.8.8.8"]

    await PinnedBackend(resolver=resolve, inner=net).connect_tcp("site.example", 80)
    assert net.dialled == [(_PUBLIC, 80), ("8.8.8.8", 80)]


async def test_a_unix_socket_is_refused() -> None:
    with pytest.raises(NonPublicAddressError):
        await PinnedBackend(inner=_Net({})).connect_unix_socket("/var/run/docker.sock")


# --- WebFetcher's direct fetch, end to end ----------------------------------------------------


async def test_a_direct_fetch_connects_to_the_address_it_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dns(monkeypatch, {"site.example": [_PUBLIC]})
    net = _Net({_PUBLIC: _PAGE})
    result = await WebFetcher(network_backend=net).fetch("http://site.example/")
    assert "hello there" in result.text
    assert net.dialled == [(_PUBLIC, 80)]
    assert b"Host: site.example" in net.streams[0].written


async def test_tls_keeps_the_hostname_while_dialling_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dns(monkeypatch, {"site.example": [_PUBLIC]})
    net = _Net({_PUBLIC: _PAGE})
    await WebFetcher(network_backend=net).fetch("https://site.example/")
    assert net.dialled == [(_PUBLIC, 443)]
    assert net.streams[0].sni == "site.example"


async def test_a_name_that_rebinds_after_the_check_is_never_dialled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Public to the pre-check, loopback to the connect: the window F9 names.
    asked = _dns(monkeypatch, {"rebind.example": [_PUBLIC, "127.0.0.1"]})
    net = _Net({_PUBLIC: _PAGE, "127.0.0.1": _PAGE})
    with pytest.raises(WebFetchError):
        await WebFetcher(network_backend=net).fetch("http://rebind.example/")
    assert len(asked) >= 2  # the check AND the connect each resolved
    assert net.dialled == []


async def test_a_redirect_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dns(monkeypatch, {"site.example": [_PUBLIC], "evil.example": ["10.0.0.5"]})
    redirect = (
        b"HTTP/1.1 302 Found\r\nlocation: http://evil.example/\r\nconnection: close\r\n"
        b"content-length: 0\r\n\r\n"
    )
    net = _Net({_PUBLIC: redirect, "10.0.0.5": _PAGE})
    with pytest.raises(WebFetchError, match="non-public"):
        await WebFetcher(network_backend=net).fetch("http://site.example/")
    assert net.dialled == [(_PUBLIC, 80)]


# --- The other direct callers ride the same pin -----------------------------------------------


async def test_favicons_and_captions_never_dial_a_rebound_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    net = _Net({_PUBLIC: _PAGE, "127.0.0.1": _PAGE})
    monkeypatch.setattr(netguard, "AnyIOBackend", lambda: net)
    _dns(
        monkeypatch,
        {"icons.example": [_PUBLIC, "127.0.0.1"], "subs.example": [_PUBLIC, "127.0.0.1"]},
    )
    assert await FaviconFetcher().fetch("icons.example") is None
    track = CaptionTrack(url="https://subs.example/t.vtt", ext="vtt", kind="manual", lang="en")
    assert await fetch_caption_transcript(track) is None
    assert net.dialled == []
