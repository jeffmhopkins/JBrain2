"""The egress executor: the one place an off-box call actually fires
(docs/ASSISTANT.md "External connectors", invariant #9).

Calls run server-side, are egress-guarded (typed slots only), cached in Postgres
(reference data is near-static), and logged (connector, input hash, domain,
principal — never the payload). A connector TOOL never calls this directly: it
stages an egress Proposal first, and only enacting that Proposal runs `fetch`, so
the owner approves the exact outbound payload before it leaves the box.
"""

from typing import Any, Protocol

import httpx
import structlog

from jbrain.connectors.base import ConnectorRegistry, build_egress
from jbrain.db.session import SessionContext

log = structlog.get_logger()
_TIMEOUT = 30.0


class CacheStore(Protocol):
    """The connector cache + audit log, on RLS-scoped sessions. A protocol so the
    executor is testable without a database."""

    async def get(
        self, ctx: SessionContext, connector: str, input_hash: str, ttl_seconds: int
    ) -> dict[str, Any] | None: ...

    async def put(
        self,
        ctx: SessionContext,
        *,
        connector: str,
        input_hash: str,
        result: dict[str, Any],
        domain: str,
        ttl_seconds: int,
    ) -> None: ...

    async def record(
        self,
        ctx: SessionContext,
        *,
        connector: str,
        input_hash: str,
        domain: str,
        principal_id: str,
    ) -> None: ...


class ConnectorService:
    """Run an egress connector: guard → cache → (network) → parse, caching and
    logging the miss. Unknown/disabled connectors and undeclared params are
    rejected before any network call."""

    def __init__(
        self,
        registry: ConnectorRegistry,
        cache: CacheStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._registry = registry
        self._cache = cache
        self._transport = transport

    async def fetch(
        self,
        ctx: SessionContext,
        *,
        connector_name: str,
        params: dict[str, Any],
        principal_id: str,
    ) -> str:
        connector = self._registry.get(connector_name)
        request = build_egress(connector, params)  # the egress guard
        cached = await self._cache.get(
            ctx, connector.name, request.input_hash, connector.ttl_seconds
        )
        if cached is not None:
            return connector.parse(cached)
        data = await self._http_get(request.url, request.query)
        await self._cache.put(
            ctx,
            connector=connector.name,
            input_hash=request.input_hash,
            result=data,
            domain=connector.domain,
            ttl_seconds=connector.ttl_seconds,
        )
        await self._cache.record(
            ctx,
            connector=connector.name,
            input_hash=request.input_hash,
            domain=connector.domain,
            principal_id=principal_id,
        )
        return connector.parse(data)

    async def _http_get(self, url: str, query: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
            resp = await client.get(url, params=query)
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
        return body
