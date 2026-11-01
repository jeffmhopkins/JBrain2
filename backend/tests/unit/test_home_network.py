"""The house's DNS, as the api reads it from AdGuard Home (docs/plans/HOME_DNS_PLAN.md).

AdGuard is faked at the transport. What is pinned is what the screen and a terminal-less
session rely on: the api authenticates as `jbrain` with the minted token (and never with
a missing one), a device filter is an exact match, AdGuard's shapes come back flat, and a
box without AdGuard reads as a 503 with why.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import home_network
from jbrain.homenet import adguard

SETTINGS: Any = SimpleNamespace(adguard_url="http://host.docker.internal:3053")
NO_DNS: Any = SimpleNamespace(adguard_url="")
OWNER: Any = SimpleNamespace(id="o", label="owner", kind="owner")

STATUS = {
    "running": True,
    "protection_enabled": True,
    "dns_addresses": ["192.168.1.50"],
    "version": "v0.107.79",
}
STATS = {
    "num_dns_queries": 1200,
    "num_blocked_filtering": 300,
    "top_clients": [{"192.168.1.30": 900}, {"192.168.1.31": 300}],
    "top_blocked_domains": [{"samsungads.com": 120}, {"lgtvsdp.com": 80}],
    "top_queried_domains": [{"netflix.com": 200}],
}
CLIENTS = {
    "clients": [{"name": "Living room TV", "ids": ["192.168.1.30"]}],
    "auto_clients": [
        {"ip": "192.168.1.30", "name": "samsung-tv.lan", "source": "rdns"},
        {"ip": "192.168.1.31", "name": "roku.lan", "source": "rdns"},
    ],
}
LOG = {
    "oldest": "2026-11-01T10:00:00Z",
    "data": [
        {
            "time": "2026-11-01T11:00:00Z",
            "client": "192.168.1.30",
            "client_info": {"name": "Living room TV"},
            "question": {"name": "samsungads.com", "type": "A"},
            "reason": "FilteredBlackList",
            "rules": [{"text": "||samsungads.com^", "filter_list_id": 2}],
        },
        {
            "time": "2026-11-01T10:59:00Z",
            "client": "192.168.1.30",
            "question": {"name": "netflix.com", "type": "AAAA"},
            "reason": "NotFilteredNotFound",
        },
        {
            "time": "2026-11-01T10:58:00Z",
            "client": "192.168.1.31",
            "question": {"name": "www.google.com", "type": "A"},
            "reason": "FilteredSafeSearch",
        },
    ],
}


class Secrets:
    def __init__(self, token: bytes | None) -> None:
        self.token = token

    def read(self, name: str) -> bytes | None:
        assert name == "adguard"
        return self.token


def _request(token: bytes | None = b"agtok") -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(internal_secrets=Secrets(token))),
        state=SimpleNamespace(),
    )


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {
        "/control/status": httpx.Response(200, json=STATUS),
        "/control/stats": httpx.Response(200, json=STATS),
        "/control/clients": httpx.Response(200, json=CLIENTS),
        "/control/querylog": httpx.Response(200, json=LOG),
        "/control/protection": httpx.Response(200),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies[request.url.path]

    monkeypatch.setattr(adguard, "_transport", httpx.MockTransport(handler))
    return seen, replies


async def test_summary_authenticates_as_jbrain_with_the_minted_token(dns) -> None:
    seen, _ = dns
    out = await home_network.home_network(_request(), SETTINGS, OWNER)
    expected = "Basic " + base64.b64encode(b"jbrain:agtok").decode()
    assert {r.headers["authorization"] for r in seen} == {expected}
    assert out["protection_enabled"] is True
    assert (out["queries"], out["blocked"]) == (1200, 300)


async def test_summary_names_devices_preferring_the_owners_name(dns) -> None:
    out = await home_network.home_network(_request(), SETTINGS, OWNER)
    assert out["devices"] == [
        {"ip": "192.168.1.30", "name": "Living room TV", "queries": 900},
        {"ip": "192.168.1.31", "name": "roku.lan", "queries": 300},
    ]
    assert out["top_blocked"][0] == {"domain": "samsungads.com", "count": 120}
    assert out["top_queried"] == [{"domain": "netflix.com", "count": 200}]


async def test_queries_for_one_device_match_it_exactly(dns) -> None:
    seen, _ = dns
    out = await home_network.home_network_queries(
        _request(), SETTINGS, OWNER, client="192.168.1.2", blocked=True, limit=50
    )
    params = seen[0].url.params
    assert params["search"] == '"192.168.1.2"'
    assert params["response_status"] == "blocked"
    assert params["limit"] == "50"
    assert "older_than" not in params
    assert out["oldest"] == "2026-11-01T10:00:00Z"


async def test_query_rows_are_flat_and_say_what_was_blocked(dns) -> None:
    out = await home_network.home_network_queries(
        _request(), SETTINGS, OWNER, older_than="2026-11-01T12:00:00Z"
    )
    first, second, third = out["rows"]
    assert first == {
        "time": "2026-11-01T11:00:00Z",
        "client": "192.168.1.30",
        "client_name": "Living room TV",
        "domain": "samsungads.com",
        "type": "A",
        "blocked": True,
        "reason": "FilteredBlackList",
        "rule": "||samsungads.com^",
    }
    assert second["blocked"] is False and second["client_name"] == ""
    assert third["blocked"] is False, "a SafeSearch rewrite is not a block"


async def test_pausing_protection_sends_a_duration(dns) -> None:
    seen, replies = dns
    replies["/control/status"] = httpx.Response(200, json={"protection_enabled": False})
    out = await home_network.home_network_protection(
        _request(), SETTINGS, OWNER, home_network.ProtectionIn(enabled=False, minutes=10)
    )
    sent = next(r for r in seen if r.url.path == "/control/protection")
    assert json.loads(sent.content) == {"enabled": False, "duration": 600_000}
    assert out == {"protection_enabled": False}


async def test_turning_protection_on_sends_no_duration(dns) -> None:
    seen, _ = dns
    await home_network.home_network_protection(
        _request(), SETTINGS, OWNER, home_network.ProtectionIn(enabled=True, minutes=10)
    )
    sent = next(r for r in seen if r.url.path == "/control/protection")
    assert json.loads(sent.content) == {"enabled": True}


async def test_debug_routes_read_the_same(dns) -> None:
    req = _request()
    out = await home_network.debug_home_network(req, SETTINGS, OWNER)
    assert out["devices"][0]["ip"] == "192.168.1.30"
    rows = await home_network.debug_home_network_queries(
        req, SETTINGS, OWNER, client="192.168.1.31"
    )
    assert len(rows["rows"]) == 3
    assert req.state.debug_detail == "home network queries 192.168.1.31"


async def test_no_token_is_a_503_and_nothing_is_sent(dns) -> None:
    seen, _ = dns
    with pytest.raises(HTTPException) as err:
        await home_network.home_network(_request(token=None), SETTINGS, OWNER)
    assert err.value.status_code == 503
    assert seen == []


async def test_no_url_is_a_503(dns) -> None:
    with pytest.raises(HTTPException) as err:
        await home_network.home_network(_request(), NO_DNS, OWNER)
    assert err.value.status_code == 503


async def test_a_refused_password_is_a_502(dns) -> None:
    _, replies = dns
    replies["/control/status"] = httpx.Response(401, text="")
    with pytest.raises(HTTPException) as err:
        await home_network.home_network(_request(), SETTINGS, OWNER)
    assert err.value.status_code == 502


async def test_an_adguard_error_is_a_502(dns) -> None:
    _, replies = dns
    replies["/control/status"] = httpx.Response(500, text="boom")
    with pytest.raises(HTTPException) as err:
        await home_network.home_network(_request(), SETTINGS, OWNER)
    assert err.value.status_code == 502


async def test_an_unreachable_adguard_is_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(adguard, "_transport", httpx.MockTransport(handler))
    with pytest.raises(HTTPException) as err:
        await home_network.home_network(_request(), SETTINGS, OWNER)
    assert err.value.status_code == 503
    assert "unreachable" in str(err.value.detail)


def test_shapes_tolerate_empty_answers() -> None:
    assert adguard.summary({}, {}, {})["devices"] == []
    assert adguard.query_rows({}) == {"rows": [], "oldest": ""}
    assert adguard.query_rows({"data": [{}]})["rows"][0]["blocked"] is False


def test_a_quote_in_the_device_filter_is_refused() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from jbrain.api.deps import get_settings, owner_only

    app = FastAPI()
    app.include_router(home_network.router, prefix="/api")
    app.dependency_overrides[owner_only] = lambda: OWNER
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    resp = TestClient(app).get("/api/home-network/queries", params={"client": 'a"b'})
    assert resp.status_code == 422
