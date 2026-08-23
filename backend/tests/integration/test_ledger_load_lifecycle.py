"""The reservation ledger against a real load lifecycle and a real Postgres.

The unit tests prove the arithmetic and the RLS test proves the storage; this proves the
WIRING — that a load charges a row, a finished load leaves it RESIDENT, a failed one leaves
nothing behind, and an unload releases it only once llama-swap has confirmed the process is
reaped. Those four are what make the ledger's numbers mean anything, and none of them can be
checked without both halves present.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.admission import Phase
from jbrain.llm.ledger import ReservationLedger
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

MODEL = "qwen3-vl-30b-a3b"


@pytest.fixture(autouse=True)
def _a_121gb_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine, so these assertions do not depend on what the CI runner has free."""
    monkeypatch.setattr(
        "jbrain.llm.ledger.read_memory_gb", lambda path="/proc/meminfo": (121.0, 0.0)
    )


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _empty_ledger(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        await session.execute(text("DELETE FROM app.model_reservations"))
    yield


def _gateway(maker: async_sessionmaker, handler: object) -> LocalGatewayClient:
    return LocalGatewayClient(
        "http://gw",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        reservations=ReservationLedger(maker, source="api"),
    )


async def test_a_completed_load_leaves_one_RESIDENT_reservation(
    maker: async_sessionmaker,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    gateway = _gateway(maker, handler)
    await gateway.load(MODEL)

    rows = await ReservationLedger(maker, source="api").live()
    assert [(r.served_model, r.phase) for r in rows] == [(MODEL, Phase.RESIDENT)]
    assert rows[0].host_gb > 0 and rows[0].device_gb > 0, "a charge of zero protects nothing"
    assert rows[0].host_gb >= rows[0].device_gb, "device must be a subset of host"


async def test_a_failed_load_leaves_NOTHING_charged(maker: async_sessionmaker) -> None:
    """A charge with no process behind it shrinks the budget permanently, and the box then
    slowly refuses everything. The TTL sweep is the backstop for a process that dies before it
    can release; a load that merely FAILS must release its own."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(500)
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    gateway = _gateway(maker, handler)
    with pytest.raises(LocalGatewayError):
        await gateway.load(MODEL)

    assert await ReservationLedger(maker, source="api").live() == []


async def test_an_unload_releases_the_charge_only_on_the_confirmed_200(
    maker: async_sessionmaker,
) -> None:
    """DRAINING keeps the FULL charge. llama-swap's unload blocks until each targeted process
    has stopped, so a 200 is the one moment this codebase can honestly say the memory is back —
    and a failure leaves the rows charged rather than optimistically released, which is the
    anti-pattern that OOMs the box."""
    unload_ok = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/unload/" in request.url.path and not unload_ok:
            return httpx.Response(500)
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    gateway = _gateway(maker, handler)
    await gateway.load(MODEL)

    with pytest.raises(LocalGatewayError):
        await gateway.unload(MODEL)
    still = await ReservationLedger(maker, source="api").live()
    assert [(r.served_model, r.phase) for r in still] == [(MODEL, Phase.DRAINING)], (
        "a failed unload released memory that may still be held"
    )
    assert still[0].host_gb > 0, "draining was discounted"

    unload_ok = True
    await gateway.unload(MODEL)
    assert await ReservationLedger(maker, source="api").live() == []


async def test_the_ledger_in_shadow_never_refuses_a_load(maker: async_sessionmaker) -> None:
    """The wave's safety property, asserted through the real load path.

    A box already charged past its capacity must still load — the ledger is being characterised,
    not obeyed. If this fails, the ledger has started governing a box nobody has checked its
    arithmetic against."""
    full = ReservationLedger(maker, source="worker")
    await full.charge("gpt-oss-120b", host_gb=115.0, device_gb=115.0)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _gateway(maker, handler).load(MODEL)  # must not raise
    assert len(await full.live()) == 2
