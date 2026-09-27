"""The push nudge: the address it picks, the contract it shares with the firmware, and the
promise that it never costs a delivery."""

from __future__ import annotations

import pathlib
import re
import socket
import time
from typing import Any

from jbrain.api import nudge


def _request(headers: dict[str, str] | None = None, client: str | None = "10.0.0.5") -> Any:
    class _Client:
        def __init__(self, host: str) -> None:
            self.host = host

    class _Req:
        def __init__(self) -> None:
            self.headers = headers or {}
            self.client = _Client(client) if client else None

    return _Req()


class TestTheAddressItPicks:
    """THE ONE THAT WOULD FAIL SILENTLY. Panels reach the api through Caddy, so the socket
    address is a container on the compose network. Firing a nudge there sends it to the reverse
    proxy — no error, no panel woken, and the only symptom is that push "does not seem faster",
    which is indistinguishable from a quiet house."""

    def test_the_forwarded_client_wins_over_the_socket(self) -> None:
        r = _request({"x-forwarded-for": "192.168.1.42"}, client="172.22.0.4")
        assert nudge._client_ip(r) == "192.168.1.42"

    def test_the_first_hop_is_the_client(self) -> None:
        """`X-Forwarded-For` accumulates left to right, so the ORIGIN is first and every entry
        after it is a proxy. Taking the last one is the classic reading of this header
        backwards, and it would aim every nudge at the nearest hop."""
        r = _request({"x-forwarded-for": "192.168.1.42, 172.22.0.4, 10.1.1.1"})
        assert nudge._client_ip(r) == "192.168.1.42"

    def test_whitespace_around_the_hop_is_not_part_of_the_address(self) -> None:
        r = _request({"x-forwarded-for": "  192.168.1.42 , 172.22.0.4"})
        assert nudge._client_ip(r) == "192.168.1.42"

    def test_it_falls_back_to_the_socket_when_nothing_forwarded(self) -> None:
        assert nudge._client_ip(_request({}, client="192.168.1.7")) == "192.168.1.7"

    def test_an_empty_header_does_not_beat_the_socket(self) -> None:
        """An empty or whitespace-only header is a proxy misconfiguration, not an address.
        Returning "" from here would poison the cache with an entry that can never be fired
        at and would never be refreshed, because a remembered address stops nothing."""
        assert nudge._client_ip(_request({"x-forwarded-for": "  "}, client="192.168.1.7")) == (
            "192.168.1.7"
        )

    def test_no_client_and_no_header_is_no_address(self) -> None:
        assert nudge._client_ip(_request({}, client=None)) is None


class TestItNeverCostsADelivery:
    """Every failure is swallowed by design: a nudge that does not arrive costs latency, and
    the slow poll is still underneath it. A nudge that RAISED would cost a child's message."""

    def setup_method(self) -> None:
        nudge._seen.clear()

    def test_an_unknown_device_is_not_an_error(self) -> None:
        assert nudge.fire("never-seen", why="test") is False

    def test_a_stale_address_is_not_fired_at(self) -> None:
        """A panel unheard of for an hour has very likely been unplugged or given a new lease,
        and firing at its old address is how one device's traffic reaches another on the same
        subnet."""
        nudge._seen["d"] = ("192.168.1.42", time.monotonic() - nudge._STALE_S - 1)
        assert nudge.fire("d", why="test") is False

    def test_a_socket_failure_is_swallowed(self, monkeypatch: Any) -> None:
        nudge._seen["d"] = ("192.168.1.42", time.monotonic())

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("network unreachable")

        monkeypatch.setattr(socket, "socket", _boom)
        assert nudge.fire("d", why="test") is False

    def test_remembering_needs_both_a_device_and_an_address(self) -> None:
        nudge.remember("", _request({"x-forwarded-for": "192.168.1.42"}))
        nudge.remember("d", _request({}, client=None))
        assert nudge._seen == {}

    def test_fire_all_reports_how_many_it_reached(self, monkeypatch: Any) -> None:
        """The count is the caller's only evidence: `report-now` says "nudged N panels", and a
        zero there is the difference between "the box told them" and "the box knows nobody"."""
        monkeypatch.setattr(nudge, "fire", lambda device_id, why: device_id != "b")
        nudge._seen.update({"a": ("1.1.1.1", time.monotonic()), "b": ("2.2.2.2", time.monotonic())})
        assert nudge.fire_all(why="test") == 1


class TestBothEndsAgreeOnTheWireFormat:
    """A CONTRACT SPELLED TWICE, which on this project has already gone wrong twice — the
    firmware called the wrong path for a release, and `tap` was sent for months into a model
    that dropped it. A port or magic that drifts here fails the same quiet way: the datagram
    is simply never received, and push degrades to the poll it was meant to replace."""

    def _firmware_header(self) -> str:
        return (
            pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / "nudge.h"
        ).read_text(encoding="utf-8")

    def test_the_port_matches(self) -> None:
        src = self._firmware_header()
        m = re.search(r"#define NUDGE_PORT (\d+)", src)
        assert m, "the firmware stopped defining NUDGE_PORT"
        assert int(m.group(1)) == nudge.NUDGE_PORT

    def test_the_magic_matches(self) -> None:
        src = self._firmware_header()
        m = re.search(r'#define NUDGE_MAGIC "([^"]+)"', src)
        assert m, "the firmware stopped defining NUDGE_MAGIC"
        assert m.group(1).encode() == nudge.NUDGE_MAGIC

    def test_the_declared_magic_length_is_the_real_one(self) -> None:
        """The firmware compares exactly `NUDGE_MAGIC_LEN` bytes, so a magic edited without its
        length would compare a prefix — silently accepting datagrams it should drop."""
        src = self._firmware_header()
        magic = re.search(r'#define NUDGE_MAGIC "([^"]+)"', src)
        length = re.search(r"#define NUDGE_MAGIC_LEN (\d+)", src)
        assert magic and length
        assert int(length.group(1)) == len(magic.group(1))

    def test_the_datagram_carries_no_authority(self) -> None:
        """THE SECURITY PROPERTY, pinned as a test because it is the whole reason an
        unauthenticated datagram is acceptable at all. The payload is the magic and nothing
        else; every real fact arrives over the authenticated poll that follows. The moment
        this carries something the panel ACTS on, it becomes an unauthenticated control
        channel into a child's bedroom."""
        assert nudge.NUDGE_MAGIC == b"JBN1"
        src = (
            pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / "nudge.c"
        ).read_text(encoding="utf-8")
        # The firmware's response to a datagram is to ASK, never to act on its contents.
        assert "jpanel_poll_soon();" in src, "the panel stopped answering a nudge by polling"
        assert "s.sendto(NUDGE_MAGIC" in (
            pathlib.Path(__file__).resolve().parents[3]
            / "backend"
            / "src"
            / "jbrain"
            / "api"
            / "nudge.py"
        ).read_text(encoding="utf-8"), "the box started sending something other than the magic"
