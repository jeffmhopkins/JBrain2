"""Migration 0170 against real Postgres: `model_reservations` is owner-only (CLAUDE.md rule 3),
and the ledger's charge/advance/discharge cycle holds against a real transaction.

The mandatory per-new-table RLS isolation test, plus the two properties the box's safety
actually rests on: an admitted charge is VISIBLE to the next admission (that is what makes two
processes serialize at all), and a draining reservation is still charged in full.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.admission import Outcome, Phase, Pool
from jbrain.llm.ledger import ReservationLedger
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A non-owner principal: a capability token with no owner identity — app.is_owner() is false.
NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _empty_ledger(maker: async_sessionmaker) -> AsyncIterator[None]:
    """`database_url` is MODULE-scoped, so without this each test inherits the previous one's
    charges — and a ledger test that starts with a full box is testing the wrong thing while
    still going green for the wrong reason."""
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        await session.execute(text("DELETE FROM app.model_reservations"))
    yield


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _pool(total: float, measured: float | None = None) -> Pool:
    return Pool(total_gb=total, reserve_gb=6.0, measured_free_gb=measured)


async def test_a_charge_is_visible_to_the_next_admission(maker: async_sessionmaker) -> None:
    """The property the box's safety rests on, and the one an in-memory ledger could never
    have: a row charged by one process is read by the next admission, in the other process.
    Committed by `charge`'s own transaction, so a second admission does not have to guess."""
    ledger = ReservationLedger(maker, source="api")

    first = await ledger.charge(
        "gpt-oss-120b", host_gb=68.0, device_gb=68.0, host=_pool(121.0), device=_pool(124.0)
    )
    assert first.decision.outcome is Outcome.ADMIT
    assert first.instance_id is not None

    # A different ReservationLedger, standing in for the worker process.
    second = await ReservationLedger(maker, source="worker").charge(
        "qwen3-coder-next", host_gb=59.6, device_gb=59.6, host=_pool(121.0), device=_pool(124.0)
    )
    assert second.decision.outcome is Outcome.DEFERRED, (
        "the second process could not see the first's charge — the ledger is not shared"
    )
    assert second.instance_id is None, "a refused admission still wrote a row"


async def test_draining_keeps_the_full_charge_until_discharge(maker: async_sessionmaker) -> None:
    """`DRAINING` is not a discount. Releasing on the shutdown DECISION is the anti-pattern
    every prior-art scheduler names — the kernel has freed nothing at the moment the intent is
    recorded — and on this box the consequence is a hard freeze and a power cycle. The charge
    goes when the process is confirmed reaped, and not before."""
    ledger = ReservationLedger(maker, source="api")
    big = await ledger.charge(
        "gpt-oss-120b", host_gb=68.0, device_gb=68.0, host=_pool(121.0), device=_pool(124.0)
    )
    assert big.instance_id is not None

    await ledger.advance(big.instance_id, Phase.DRAINING)
    blocked = await ledger.charge(
        "qwen3-coder-next", host_gb=59.6, device_gb=59.6, host=_pool(121.0), device=_pool(124.0)
    )
    assert blocked.decision.outcome is Outcome.DEFERRED, "a draining model was discounted"

    await ledger.discharge(big.instance_id)
    now_free = await ledger.charge(
        "qwen3-coder-next", host_gb=59.6, device_gb=59.6, host=_pool(121.0), device=_pool(124.0)
    )
    assert now_free.decision.outcome is Outcome.ADMIT
    assert [r.served_model for r in await ledger.live()] == ["qwen3-coder-next"]


async def test_reconcile_drops_a_phantom_and_reports_a_foreign_model(
    maker: async_sessionmaker,
) -> None:
    ledger = ReservationLedger(maker, source="api")
    ghost = await ledger.charge(
        "gpt-oss-120b", host_gb=68.0, device_gb=68.0, host=_pool(121.0), device=_pool(124.0)
    )
    assert ghost.instance_id is not None

    phantoms, foreign = await ledger.reconcile({"comfyui-sdxl"})
    assert phantoms == [ghost.instance_id]
    assert foreign == ["comfyui-sdxl"]
    assert await ledger.live() == []


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    ledger = ReservationLedger(maker, source="api")
    charged = await ledger.charge(
        "gpt-oss-120b", host_gb=68.0, device_gb=68.0, host=_pool(121.0), device=_pool(124.0)
    )
    assert charged.instance_id is not None
    assert owner.principal_id  # the owner context is real, not the ledger's own machine context

    # A non-owner principal sees zero rows — RLS hides the whole table.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM app.model_reservations"))
        ).scalar()
    assert count == 0

    insert = text(
        "INSERT INTO app.model_reservations (served_model, host_gb, device_gb)"
        " VALUES ('sneaky', 1.0, 1.0)"
    )
    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await session.execute(insert)

    # The SAME statement under an owner context must succeed. Without this the test above
    # passes just as well against a typo — a malformed INSERT raises ProgrammingError too, and
    # an RLS test that cannot tell a firewall from a syntax error is not testing the firewall.
    async with scoped_session(maker, owner) as session:
        await session.execute(insert)
