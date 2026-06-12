"""The egress executor: cache miss fetches+caches+logs, cache hit skips the
network, and the egress guard rejects before any call (docs/ASSISTANT.md #9). HTTP
is faked via MockTransport — no live network, like the LLM adapter."""

from typing import Any

import httpx
import pytest

from jbrain.connectors.base import ConnectorRegistry, EgressGuardError
from jbrain.connectors.medical import medical_connectors
from jbrain.connectors.service import ConnectorService
from jbrain.db.session import SessionContext

CTX = SessionContext(principal_kind="owner")
RXNAV_OK = {
    "drugGroup": {"conceptGroup": [{"conceptProperties": [{"name": "metformin", "rxcui": "6809"}]}]}
}


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict[str, Any]] = {}
        self.logs: list[tuple[str, str]] = []
        self.puts = 0

    async def get(
        self, ctx: object, connector: str, input_hash: str, ttl_seconds: int
    ) -> dict[str, Any] | None:
        return self.store.get((connector, input_hash))

    async def put(self, ctx: object, *, connector: str, input_hash: str, **kw: Any) -> None:
        self.store[(connector, input_hash)] = kw["result"]
        self.puts += 1

    async def record(self, ctx: object, *, connector: str, principal_id: str, **kw: Any) -> None:
        self.logs.append((connector, principal_id))


def service(cache: FakeCache, calls: list[httpx.Request]) -> ConnectorService:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=RXNAV_OK)

    registry = ConnectorRegistry(medical_connectors("https://rxnav.example", "https://mp.example"))
    return ConnectorService(registry, cache, transport=httpx.MockTransport(handle))  # type: ignore[arg-type]


async def test_cache_miss_fetches_caches_and_logs() -> None:
    cache, calls = FakeCache(), []
    out = await service(cache, calls).fetch(
        CTX, connector_name="lookup_medication", params={"name": "metformin"}, principal_id="p1"
    )
    assert "metformin (rxcui 6809)" in out
    assert len(calls) == 1  # one outbound call
    assert calls[0].url.params["name"] == "metformin"  # typed slot only
    assert cache.puts == 1 and cache.logs == [("lookup_medication", "p1")]


async def test_cache_hit_skips_the_network() -> None:
    cache, calls = FakeCache(), []
    svc = service(cache, calls)
    await svc.fetch(
        CTX, connector_name="lookup_medication", params={"name": "metformin"}, principal_id="p1"
    )
    calls.clear()
    out = await svc.fetch(
        CTX, connector_name="lookup_medication", params={"name": "metformin"}, principal_id="p1"
    )
    assert calls == []  # served from cache, no second call
    assert "metformin" in out


async def test_an_undeclared_param_never_reaches_the_network() -> None:
    cache, calls = FakeCache(), []
    with pytest.raises(EgressGuardError, match="undeclared"):
        await service(cache, calls).fetch(
            CTX,
            connector_name="lookup_medication",
            params={"name": "x", "ssn": "123-45-6789"},
            principal_id="p1",
        )
    assert calls == []  # the guard rejected it before any egress


async def test_an_unknown_connector_is_rejected() -> None:
    cache, calls = FakeCache(), []
    with pytest.raises(EgressGuardError, match="unknown connector"):
        await service(cache, calls).fetch(
            CTX, connector_name="evil_fetch", params={}, principal_id="p1"
        )
    assert calls == []
