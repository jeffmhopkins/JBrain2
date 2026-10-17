"""Who the client is, through Caddy and the tunnel — and who it is not (S3.4).

The matrix that matters: Caddy's `X-Real-IP` is believed only from Caddy, Cloudflare's
`Cf-Connecting-IP` only when the hop before Caddy is the connector, and anything that cannot be
resolved trusts nothing. Then the limiters that key on it, which used to share one bucket.
"""

from __future__ import annotations

import ast
import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from jbrain.agent import chat_shares
from jbrain.api import chat_share, jlaunch_share
from jbrain.api import client_ip as cip
from jbrain.config import Settings
from jbrain.external import jlaunch_shares
from jbrain.locations.ratelimit import TokenBucket
from jbrain.main import create_app
from tests.unit.fakes import FakePairingRepo

PROXY = "172.18.0.3"
TUNNEL = "172.18.0.9"
GATEWAY = "172.18.0.1"


def _conn(peer: str | None, **headers: str) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 40000) if peer else None,
    }
    return Request(scope)


class TestTheMatrix:
    def test_direct_mode_reads_the_address_caddy_saw(self) -> None:
        with cip.pinned(proxy=(PROXY,)):
            assert cip.client_ip(_conn(PROXY, x_real_ip="203.0.113.7")) == "203.0.113.7"

    def test_tunnel_mode_reads_cloudflares_client(self) -> None:
        with cip.pinned(proxy=(PROXY,), tunnel=(TUNNEL,)):
            conn = _conn(PROXY, x_real_ip=TUNNEL, cf_connecting_ip="198.51.100.4")
            assert cip.client_ip(conn) == "198.51.100.4"

    def test_a_lan_client_cannot_spoof_cloudflare_through_the_published_port(self) -> None:
        """Direct-mode or tunnel-mode alike: the hop before Caddy is the client itself (or the
        docker gateway a published-port connection arrives from), not the connector, so its
        `Cf-Connecting-IP` is ignored."""
        with cip.pinned(proxy=(PROXY,), tunnel=(TUNNEL,)):
            for hop in ("192.168.1.50", GATEWAY):
                conn = _conn(PROXY, x_real_ip=hop, cf_connecting_ip="1.2.3.4")
                assert cip.client_ip(conn) == hop
        with cip.pinned(proxy=(PROXY,)):
            conn = _conn(PROXY, x_real_ip="192.168.1.50", cf_connecting_ip="1.2.3.4")
            assert cip.client_ip(conn) == "192.168.1.50"

    def test_a_sandbox_peer_cannot_forge_x_real_ip(self) -> None:
        """A jcode or pysandbox container reaches the api directly, so it is its own peer."""
        with cip.pinned(proxy=(PROXY,), tunnel=(TUNNEL,)):
            conn = _conn("172.30.0.5", x_real_ip=TUNNEL, cf_connecting_ip="1.2.3.4")
            assert cip.client_ip(conn) == "172.30.0.5"

    def test_nothing_resolved_trusts_nothing(self) -> None:
        with cip.pinned():
            assert cip.client_ip(_conn(PROXY, x_real_ip="203.0.113.7")) == PROXY

    def test_a_garbage_header_falls_back_to_the_peer(self) -> None:
        with cip.pinned(proxy=(PROXY,), tunnel=(TUNNEL,)):
            assert cip.client_ip(_conn(PROXY, x_real_ip="not an ip")) == PROXY
            assert cip.client_ip(_conn(PROXY)) == PROXY
            conn = _conn(PROXY, x_real_ip=TUNNEL, cf_connecting_ip="nope")
            assert cip.client_ip(conn) == TUNNEL

    def test_an_ipv4_mapped_peer_still_matches(self) -> None:
        with cip.pinned(proxy=(PROXY,)):
            conn = _conn(f"::ffff:{PROXY}", x_real_ip="203.0.113.7")
            assert cip.client_ip(conn) == "203.0.113.7"

    def test_no_client_is_no_address(self) -> None:
        assert cip.client_ip(_conn(None)) is None


class TestResolution:
    @pytest.fixture(autouse=True)
    def _clean(self) -> Any:
        saved = dict(cip._resolved)
        cip._resolved.clear()
        yield
        cip._resolved.clear()
        cip._resolved.update(saved)

    def test_a_failed_lookup_falls_back_to_the_socket(self, monkeypatch: Any) -> None:
        async def boom(self: Any, host: str, *_a: Any, **_k: Any) -> Any:
            raise socket.gaierror(socket.EAI_NONAME, "no such host")

        monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", boom)
        asyncio.run(cip.refresh())
        assert cip._resolved == {cip.PROXY_SERVICE: frozenset(), cip.TUNNEL_SERVICE: frozenset()}
        assert cip.client_ip(_conn(PROXY, x_real_ip="203.0.113.7")) == PROXY

    def test_resolved_services_are_trusted(self, monkeypatch: Any) -> None:
        table = {cip.PROXY_SERVICE: PROXY, cip.TUNNEL_SERVICE: TUNNEL}

        async def lookup(self: Any, host: str, *_a: Any, **_k: Any) -> Any:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]

        monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", lookup)
        asyncio.run(cip.refresh())
        conn = _conn(PROXY, x_real_ip=TUNNEL, cf_connecting_ip="198.51.100.4")
        assert cip.client_ip(conn) == "198.51.100.4"


_DB = "postgresql+asyncpg://nobody@localhost:1/none"


class TestLimitersKeyOnTheClient:
    """Every limiter used to key on `request.client.host` — Caddy, for every request — so one
    scanner spent everyone's budget. Each client now has its own bucket, and a forged header
    from a peer that is not Caddy buys no fresh one."""

    def _pair(self, c: TestClient, ip: str) -> int:
        return c.post(
            "/api/pairing/redeem", json={"code": "x"}, headers={"X-Real-IP": ip}
        ).status_code

    def test_pairing_buckets_are_per_client(self) -> None:
        app = create_app(Settings(secure_cookies=False, database_url=_DB, mqtt_public_host="h"))
        with TestClient(app, client=(PROXY, 40000)) as c, cip.pinned(proxy=(PROXY,)):
            app.state.pairing_repo = FakePairingRepo()
            app.state.pairing_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
            assert self._pair(c, "203.0.113.1") == 400
            assert self._pair(c, "203.0.113.1") == 429
            # A second client is not throttled by the first.
            assert self._pair(c, "203.0.113.2") == 400

    def test_a_direct_peer_cannot_mint_buckets_with_a_header(self) -> None:
        app = create_app(Settings(secure_cookies=False, database_url=_DB, mqtt_public_host="h"))
        with TestClient(app, client=("172.30.0.5", 40000)) as c, cip.pinned(proxy=(PROXY,)):
            app.state.pairing_repo = FakePairingRepo()
            app.state.pairing_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
            assert self._pair(c, "203.0.113.1") == 400
            assert self._pair(c, "203.0.113.2") == 429

    def test_share_pages_key_on_the_tunnel_client(self, monkeypatch: Any) -> None:
        async def _none(_maker: Any, _token: str) -> None:
            return None

        monkeypatch.setattr(jlaunch_shares, "resolve_share", _none)
        app = FastAPI()
        app.include_router(jlaunch_share.router, prefix="/api")
        app.state.session_maker = object()
        app.state.blob_store = None
        app.state.jlaunch_share_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
        c = TestClient(app, client=(PROXY, 40000))

        def get(cf: str) -> int:
            headers = {"X-Real-IP": TUNNEL, "Cf-Connecting-IP": cf}
            return c.get("/api/jlaunch-share/tok", headers=headers).status_code

        with cip.pinned(proxy=(PROXY,), tunnel=(TUNNEL,)):
            assert get("198.51.100.1") == 404
            assert get("198.51.100.1") == 429
            assert get("198.51.100.2") == 404

    def test_chat_share_keys_on_the_client(self, monkeypatch: Any) -> None:
        async def _none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(chat_shares, "resolve_share", _none)
        app = FastAPI()
        app.include_router(chat_share.router, prefix="/api")
        app.state.session_maker = object()
        app.state.chat_share_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
        c = TestClient(app, client=(PROXY, 40000))

        def get(ip: str) -> int:
            return c.get("/api/chat-share/tok", headers={"X-Real-IP": ip}).status_code

        with cip.pinned(proxy=(PROXY,)):
            assert get("203.0.113.1") == 404
            assert get("203.0.113.1") == 429
            assert get("203.0.113.2") == 404


def test_no_api_module_keys_on_the_raw_socket_address() -> None:
    # Behind Caddy `request.client.host` is the proxy for every request, so a limiter keyed
    # on it is one global bucket. Only the helper itself (and the panel tunnel's transport,
    # which forwards the peer verbatim) may read it.
    api = Path(__file__).resolve().parents[2] / "src" / "jbrain" / "api"
    allowed = {"client_ip.py", "panel_ws.py"}
    offenders = []
    for path in api.glob("*.py"):
        if path.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "host"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "client"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


class TestIngressAndDevice:
    """`X-JBrain-Ingress` and `X-JBrain-Device` (OWNER_TUNNEL_PLAN §3.2): believed only from
    Caddy, and the device only on the owner site."""

    KEY = "ab" * 32

    def test_each_site_is_read_from_caddy(self) -> None:
        with cip.pinned(proxy=(PROXY,)):
            for name in ("public", "lan", "owner-tunnel", "preview"):
                assert cip.ingress(_conn(PROXY, x_jbrain_ingress=name)) == name

    def test_not_from_caddy_is_no_ingress(self) -> None:
        with cip.pinned(proxy=(PROXY,)):
            conn = _conn("172.20.0.5", x_jbrain_ingress="owner-tunnel", x_jbrain_device=self.KEY)
            assert cip.ingress(conn) is None
            assert cip.device_key(conn) is None
            assert cip.ingress(_conn(None, x_jbrain_ingress="lan")) is None

    def test_unknown_or_missing_ingress_is_none(self) -> None:
        with cip.pinned(proxy=(PROXY,)):
            assert cip.ingress(_conn(PROXY, x_jbrain_ingress="admin")) is None
            assert cip.ingress(_conn(PROXY)) is None

    def test_device_only_on_the_owner_site(self) -> None:
        with cip.pinned(proxy=(PROXY,)):
            owner = _conn(PROXY, x_jbrain_ingress="owner-tunnel", x_jbrain_device=self.KEY)
            assert cip.device_key(owner) == self.KEY
            lan = _conn(PROXY, x_jbrain_ingress="lan", x_jbrain_device=self.KEY)
            assert cip.device_key(lan) is None

    @pytest.mark.parametrize("value", ["", "AB" * 32, "ab" * 31, "zz" * 32, "ab" * 33])
    def test_a_malformed_device_is_none(self, value: str) -> None:
        with cip.pinned(proxy=(PROXY,)):
            conn = _conn(PROXY, x_jbrain_ingress="owner-tunnel", x_jbrain_device=value)
            assert cip.device_key(conn) is None
