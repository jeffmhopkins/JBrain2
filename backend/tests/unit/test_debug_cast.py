"""The debug console's window onto the house's Chromecast TVs (the jpanel Cast spike).

Thin proxies, so what is pinned is what a session with no terminal relies on: the api
presents `cast.token` (never a missing one), a gate refusal arrives as a 409 carrying the
verdict that refused it, and an unreachable or unconfigured sidecar reads as a state.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import debug_cast

TV = "0f4e6b0a-6c1e-4b0a-9a5e-3f1a2b3c4d5e"
SETTINGS: Any = SimpleNamespace(cast_url="http://host.docker.internal:19190")
NO_SIDECAR: Any = SimpleNamespace(cast_url="")
PRINCIPAL: Any = SimpleNamespace(id="x", label="l", kind="capability_token")


class Secrets:
    def __init__(self, token: bytes | None) -> None:
        self.token = token

    def read(self, name: str) -> bytes | None:
        assert name == "cast"
        return self.token


def _request(token: bytes | None = b"cast-tok") -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(internal_secrets=Secrets(token))),
        state=SimpleNamespace(),
    )


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.get(request.url.path, httpx.Response(200, json={"tvs": []}))

    monkeypatch.setattr(debug_cast, "_transport", httpx.MockTransport(handler))
    return seen, replies


async def test_lists_the_tvs_with_the_minted_token(sidecar) -> None:
    seen, replies = sidecar
    replies["/tvs"] = httpx.Response(200, json={"tvs": [{"id": TV, "state": "idle"}]})
    out = await debug_cast.cast_tvs(_request(), SETTINGS, PRINCIPAL)
    assert out["tvs"][0]["state"] == "idle"
    assert seen[0].headers["authorization"] == "Bearer cast-tok"


async def test_history_passes_since_through(sidecar) -> None:
    seen, _ = sidecar
    await debug_cast.cast_history(_request(), SETTINGS, PRINCIPAL, TV, since=12.5)
    assert seen[0].url.path == f"/tvs/{TV}/history"
    assert seen[0].url.params["since"] == "12.5"


async def test_a_gate_refusal_is_a_409_carrying_the_verdict(sidecar) -> None:
    seen, replies = sidecar
    replies[f"/tvs/{TV}/test"] = httpx.Response(
        409, json={"error": "not idle", "state": "busy", "why": "Netflix · playing"}
    )
    with pytest.raises(HTTPException) as err:
        await debug_cast.cast_test(
            _request(), SETTINGS, PRINCIPAL, TV, debug_cast.CastTestIn(seconds=20)
        )
    assert err.value.status_code == 409
    detail: Any = err.value.detail
    assert detail["why"] == "Netflix · playing"
    assert json.loads(seen[0].content) == {"seconds": 20}


async def test_a_test_cast_returns_what_the_sidecar_started(sidecar) -> None:
    _, replies = sidecar
    replies[f"/tvs/{TV}/test"] = httpx.Response(202, json={"id": TV, "state_before": "standby"})
    out = await debug_cast.cast_test(_request(), SETTINGS, PRINCIPAL, TV, debug_cast.CastTestIn())
    assert out["state_before"] == "standby"


async def test_no_token_yet_refuses_before_any_request(sidecar) -> None:
    seen, _ = sidecar
    with pytest.raises(HTTPException) as err:
        await debug_cast.cast_tvs(_request(token=None), SETTINGS, PRINCIPAL)
    assert err.value.status_code == 503
    assert seen == []


async def test_a_token_mismatch_is_a_deploy_fault(sidecar) -> None:
    _, replies = sidecar
    replies["/tvs"] = httpx.Response(401, json={"error": "unauthorized"})
    with pytest.raises(HTTPException) as err:
        await debug_cast.cast_tvs(_request(), SETTINGS, PRINCIPAL)
    assert err.value.status_code == 502


async def test_a_sidecar_error_is_a_502(sidecar) -> None:
    _, replies = sidecar
    replies["/tvs"] = httpx.Response(500, text="boom")
    with pytest.raises(HTTPException) as err:
        await debug_cast.cast_tvs(_request(), SETTINGS, PRINCIPAL)
    assert err.value.status_code == 502


async def test_unreachable_and_unconfigured_read_as_states(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(debug_cast, "_transport", httpx.MockTransport(refuse))
    with pytest.raises(HTTPException) as err:
        await debug_cast.cast_tvs(_request(), SETTINGS, PRINCIPAL)
    assert err.value.status_code == 503
    with pytest.raises(HTTPException) as err:
        await debug_cast.cast_tvs(_request(), NO_SIDECAR, PRINCIPAL)
    assert err.value.status_code == 503
