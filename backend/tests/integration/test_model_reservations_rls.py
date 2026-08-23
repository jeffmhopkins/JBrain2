"""Migration 0170 against real Postgres: `model_reservations` is owner-only (CLAUDE.md rule 3),
and the ledger's charge/advance/discharge cycle holds against a real transaction.

The mandatory per-new-table RLS isolation test, plus the two properties the box's safety
actually rests on: an admitted charge is VISIBLE to the next admission (that is what makes two
processes serialize at all), and a draining reservation is still charged in full.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.admission import Outcome, Phase
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
def _a_121gb_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine these tests reason about. The ledger reads real memory now, so without
    this every assertion below would depend on how much the CI runner happens to have free —
    and a 68 GB model is DEFERRED on any of them, which would make the tests pass for the wrong
    reason. 121 GB total and nothing used is the box with its models unloaded."""
    monkeypatch.setattr(
        "jbrain.llm.ledger.read_memory_gb", lambda path="/proc/meminfo": (121.0, 0.0)
    )


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


async def test_a_charge_is_visible_to_the_next_admission(maker: async_sessionmaker) -> None:
    """The property the box's safety rests on, and the one an in-memory ledger could never
    have: a row charged by one process is read by the next admission, in the other process.
    Committed by `charge`'s own transaction, so a second admission does not have to guess."""
    ledger = ReservationLedger(maker, source="api", shadow=False)

    first = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert first.decision.outcome is Outcome.ADMIT
    assert first.instance_id is not None

    # A different ReservationLedger, standing in for the worker process.
    second = await ReservationLedger(maker, source="worker", shadow=False).charge(
        "qwen3-coder-next", host_gb=59.6, device_gb=59.6
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
    ledger = ReservationLedger(maker, source="api", shadow=False)
    big = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert big.instance_id is not None

    await ledger.advance(big.instance_id, Phase.DRAINING)
    blocked = await ledger.charge("qwen3-coder-next", host_gb=59.6, device_gb=59.6)
    assert blocked.decision.outcome is Outcome.DEFERRED, "a draining model was discounted"

    await ledger.discharge(big.instance_id)
    now_free = await ledger.charge("qwen3-coder-next", host_gb=59.6, device_gb=59.6)
    assert now_free.decision.outcome is Outcome.ADMIT
    assert [r.served_model for r in await ledger.live()] == ["qwen3-coder-next"]


async def test_reconcile_drops_a_settled_phantom_and_reports_a_foreign_model(
    maker: async_sessionmaker,
) -> None:
    """A row that REACHED the box and whose model is now gone is a charge nobody will release.
    Left alone it shrinks the budget permanently and the box slowly refuses everything.

    The foreign model is reported and NOT charged: inventing a declaration for a process we did
    not admit would put a number we made up into the arithmetic that protects the box."""
    ledger = ReservationLedger(maker, source="api", shadow=False)
    ghost = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert ghost.instance_id is not None
    await ledger.advance(ghost.instance_id, Phase.RESIDENT)

    phantoms, foreign = await ledger.reconcile({"comfyui-sdxl"})
    assert phantoms == [ghost.instance_id]
    assert foreign == ["comfyui-sdxl"]
    assert await ledger.live() == []


async def test_a_lost_reservation_is_re_charged_when_the_model_goes_resident(
    maker: async_sessionmaker,
) -> None:
    """The control that turns a deleted reservation from silent corruption into a loud repair.

    `advance` used to update zero rows in silence. That silence is the whole danger: the load
    carries on, the model becomes resident, and the ledger charges nothing for it — a number
    that is confidently wrong, which is worse than no ledger. Advancing to RESIDENT is the
    moment the memory becomes permanent, so this case re-charges at the ORIGINAL declaration
    rather than merely complaining. The box is holding it either way."""
    ledger = ReservationLedger(maker, source="worker", shadow=False)
    charge = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert charge.instance_id is not None

    # Something deleted it underneath a live load — a stray sweep, an operator, a bug.
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        await session.execute(text("DELETE FROM app.model_reservations"))
    assert await ledger.live() == []

    await ledger.advance(charge.instance_id, Phase.RESIDENT)

    back = await ledger.live()
    assert [(r.served_model, r.host_gb, r.phase) for r in back] == [
        ("gpt-oss-120b", 68.0, Phase.RESIDENT)
    ], f"the ledger forgot 68 GB the box is actually holding: {back}"


async def test_reconcile_leaves_the_OTHER_processes_live_charge_alone(
    maker: async_sessionmaker,
) -> None:
    """The startup race that would have made reconciliation worse than none.

    Both the api and the worker reconcile at startup. The worker charges a row and begins a
    200 s load, during which the model is not yet in `/running`. The api restarts. A whole-table
    sweep reads that row as a phantom and deletes it — and the box is now loading 69 GB with no
    reservation behind it, which is the exact condition this ledger exists to make impossible.

    A row is a phantom because the process that charged it died, and only its own successor
    knows that. The other process's dead rows are cleaned by its own restart, and by the TTL
    sweep meanwhile."""
    worker = ReservationLedger(maker, source="worker", shadow=False)
    mid_load = await worker.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert mid_load.instance_id is not None
    await worker.advance(mid_load.instance_id, Phase.STARTING)

    # The api comes up and reconciles against a box where that model is not resident YET.
    phantoms, foreign = await ReservationLedger(maker, source="api", shadow=False).reconcile(set())

    assert phantoms == [], "the api deleted the worker's live charge"
    assert foreign == [], "a model the other process charged was reported as unaccounted-for"
    assert [r.served_model for r in await worker.live()] == ["gpt-oss-120b"]


async def test_reconcile_spares_a_row_that_has_not_reached_the_box_yet(
    maker: async_sessionmaker,
) -> None:
    """PLANNED and STARTING look exactly like a load in progress, because that is what they are.

    A cold load of a 69 GB model measured 198 s on this box, and for all of it the model is
    absent from `/running`. Sweeping those rows is the failure the phase filter exists for. And
    it is NOT enough to filter by `source`: llama-swap owns the model processes, so neither the
    api nor the worker restarting kills a model — "my process restarted, therefore my rows are
    dead" is false here in both directions."""
    ledger = ReservationLedger(maker, source="api", shadow=False)
    planned = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert planned.instance_id is not None

    phantoms, _ = await ledger.reconcile(set())
    assert phantoms == [], "a load in progress was swept out from under itself"

    await ledger.advance(planned.instance_id, Phase.STARTING)
    phantoms, _ = await ledger.reconcile(set())
    assert phantoms == [], "a model reading its weights was swept out from under itself"
    assert len(await ledger.live()) == 1


async def test_shadow_mode_charges_the_row_it_would_have_refused(
    maker: async_sessionmaker,
) -> None:
    """The wave's whole safety property: while the ledger is in shadow it never says no.

    A ledger that has never charged a real load has no numbers to be judged on, and this repo
    has the precedent and the reason — `_note_not_ready` was landed as measurement first,
    "because the fix removes the thing being measured, so shipping both together would only
    ever report zero". So the lifecycle runs for real, the verdict is recorded next to what the
    live gate actually did, and the box is observed before it is governed.

    If this test ever fails, the ledger has started refusing loads on a box nobody has checked
    its arithmetic against."""
    shadow = ReservationLedger(maker, source="api")  # the default
    first = await shadow.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert first.instance_id is not None

    # Would not fit — 68 charged out of 121 less a 6 GB reserve leaves 47.
    second = await shadow.charge("qwen3-coder-next", host_gb=59.6, device_gb=59.6)
    assert second.decision.outcome is Outcome.DEFERRED, "the arithmetic changed, not the mode"
    assert second.instance_id is not None, (
        "shadow mode refused a load — it must observe the box, never govern it"
    )
    assert len(await shadow.live()) == 2


async def test_a_NARROWED_owner_session_cannot_touch_the_memory_ledger(
    maker: async_sessionmaker,
) -> None:
    """Where this table parts company with the telemetry it otherwise copies.

    `app.is_owner()` keys on principal KIND, so an owner-NARROWED agent session — a tool job
    firewalled to one domain — satisfies it, and under the box_events policy would hold DELETE
    here. For telemetry that is a reasonable inherited default. For the table whose corruption
    is a host power cycle it is a decision, and the decision is that a narrowed session has no
    business editing the box's memory accounting. `app.is_full_owner()` is what draws that line
    — and a test is what keeps the line from being quietly redrawn by a copy-paste."""
    ledger = ReservationLedger(maker, source="api", shadow=False)
    charged = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
    assert charged.instance_id is not None

    base = await _owner(maker)
    narrowed = SessionContext(
        principal_id=base.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=("health",),
    )
    async with scoped_session(maker, narrowed) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM app.model_reservations"))
        ).scalar()
    assert count == 0, "a domain-narrowed agent session can read the box's memory ledger"

    # A DELETE is filtered by `USING`, not refused by it — it matches no visible rows and
    # succeeds as a no-op. So the property to assert is that the ledger SURVIVED, not that an
    # error was raised; asserting the error would be asserting the wrong mechanism.
    async with scoped_session(maker, narrowed) as session:
        await session.execute(text("DELETE FROM app.model_reservations"))
    assert len(await ledger.live()) == 1, "a narrowed session deleted the box's memory ledger"

    # An INSERT is refused outright, because `WITH CHECK` has no row to filter.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, narrowed) as session:
            await session.execute(
                text(
                    "INSERT INTO app.model_reservations (served_model, host_gb, device_gb)"
                    " VALUES ('smuggled', 1.0, 1.0)"
                )
            )


async def test_the_row_shape_itself_refuses_a_device_figure_larger_than_its_host_one(
    maker: async_sessionmaker,
) -> None:
    """ "Double-counting becomes unrepresentable" as a fact rather than an assertion.

    Every device byte on this hardware IS a host byte — the iGPU draws GTT from system RAM — so
    device is a subset of host, never a second pool to add on. Without the constraint a future
    writer can charge a device figure larger than its host figure, or a host figure that omits
    the device bytes, which is the exact miscount the two-columns-on-one-row shape is supposed
    to have designed away."""
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO app.model_reservations (served_model, host_gb, device_gb)"
                    " VALUES ('impossible', 10.0, 40.0)"
                )
            )


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    ledger = ReservationLedger(maker, source="api", shadow=False)
    charged = await ledger.charge("gpt-oss-120b", host_gb=68.0, device_gb=68.0)
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
