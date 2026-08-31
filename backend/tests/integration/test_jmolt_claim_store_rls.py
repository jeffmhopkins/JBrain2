"""Migration 0183 against real Postgres: what jmolt has already claimed, across nights.

The table exists because of a measurement. On the live box, six posts across 2026-08-28 and
2026-08-29 asserted one claim and the sequence crossed the night boundary — so a gate holding
only tonight's claims lets the first restatement of every night through, every night. These
tests pin that the store actually reaches back, and that jmolt cannot edit or delete what it
said, which is the only thing making the gate's memory trustworthy.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_claim import Claim
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_claim_store import ClaimStore
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

VEC = [0.1] * 384
OTHER = [0.2] * 384


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_claim"))
    yield


async def _say(maker, pid: str, claim: Claim, *, published: bool = True) -> str:
    async with scoped_session(maker, _jmolt(pid)) as s:
        return await ClaimStore().record(
            s, pid, claim, embedding=VEC, object_embedding=OTHER, published=published
        )


async def test_a_claim_survives_to_be_recalled_with_its_vectors(maker) -> None:
    """Stored, not recomputed: a night that re-embedded its own history would pay nightly and
    be at the mercy of the embedder being up — and "cannot embed history" must never mean "the
    gate lets everything through"."""
    pid = await _owner_pid(maker)
    claim = Claim.of("owner prompt", "IS", "initial seed", citations=["p1"])
    await _say(maker, pid, claim)
    async with scoped_session(maker, _jmolt(pid)) as s:
        [stored] = await ClaimStore().recent(s, pid)
    assert stored.claim == claim
    assert stored.embedding == pytest.approx(VEC)
    assert stored.object_embedding == pytest.approx(OTHER)


async def test_the_gate_loads_claims_from_before_tonight(maker) -> None:
    """The measurement this table exists for: #58 restated #53 from the night before."""
    pid = await _owner_pid(maker)
    for i in range(3):
        await _say(maker, pid, Claim.of(f"subject {i}", "IS", f"object {i}"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        recalled = await ClaimStore().recent(s, pid)
    assert len(recalled) == 3  # nothing about "tonight" bounds it


async def test_a_refused_claim_is_kept_but_never_recalled(maker) -> None:
    """Kept, because a refusal that keeps recurring is the strongest signal a threshold is
    wrong. Not recalled, because a draft that was never said is not something to repeat — and
    holding it against the night would make the gate stricter every time it refused."""
    pid = await _owner_pid(maker)
    await _say(maker, pid, Claim.of("said", "IS", "aloud"), published=True)
    await _say(maker, pid, Claim.of("refused", "IS", "a draft"), published=False)
    since = datetime.now(UTC) - timedelta(minutes=1)
    async with scoped_session(maker, _jmolt(pid)) as s:
        recalled = await ClaimStore().recent(s, pid)
        assert await ClaimStore().refusal_counts(s, pid, since=since) == 1
    assert [c.claim.subject for c in recalled] == ["said"]


async def test_recall_is_bounded_and_newest_first(maker) -> None:
    """The gate compares the candidate against every recalled claim, so an unbounded history
    makes the guard the cost."""
    pid = await _owner_pid(maker)
    for i in range(12):
        await _say(maker, pid, Claim.of(f"subject {i}", "IS", f"object {i}"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        recalled = await ClaimStore().recent(s, pid, limit=5)
    assert [c.claim.subject for c in recalled] == [f"subject {i}" for i in (11, 10, 9, 8, 7)]


async def test_nobody_can_edit_what_jmolt_said(maker) -> None:
    """Editing a claim after the fact would let a night launder a repeat into a novelty.

    Refused HARDER than the other tables: there is no UPDATE policy AND no UPDATE grant, so
    the attempt raises a privilege error rather than being a silently-zero-row no-op. That is
    the stronger of the two, and worth asserting as such — a no-op leaves a caller believing
    it succeeded."""
    pid = await _owner_pid(maker)
    await _say(maker, pid, Claim.of("owner prompt", "IS", "initial seed"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        with pytest.raises(Exception, match="permission denied"):
            await s.execute(text("UPDATE app.jmolt_claim SET object = 'something else'"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        [stored] = await ClaimStore().recent(s, pid)
    assert stored.claim.object == "initial seed"


async def test_jmolt_cannot_delete_what_it_said(maker) -> None:
    """A claim jmolt could retract is a claim it could repeat."""
    pid = await _owner_pid(maker)
    await _say(maker, pid, Claim.of("inconvenient", "IS", "on the record"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        await s.execute(text("DELETE FROM app.jmolt_claim"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert len(await ClaimStore().recent(s, pid)) == 1


async def test_jmolt_cannot_write_a_claim_keyed_to_anyone_else(maker) -> None:
    pid = await _owner_pid(maker)
    async with scoped_session(maker, _jmolt(pid)) as s:
        with pytest.raises(Exception):  # noqa: B017 — RLS refuses; the class is the driver's
            await ClaimStore().record(
                s,
                "someone-else",
                Claim.of("theirs", "IS", "not mine"),
                embedding=VEC,
                object_embedding=OTHER,
                published=True,
            )


async def test_an_outsider_sees_nothing_and_the_owner_can_prune(maker) -> None:
    pid = await _owner_pid(maker)
    await _say(maker, pid, Claim.of("private", "IS", "to this box"))
    outsider = SessionContext(principal_id="stranger", principal_kind="capability_token")
    async with scoped_session(maker, outsider) as s:
        assert await ClaimStore().recent(s, pid) == []
    async with scoped_session(maker, _admin(pid)) as s:
        assert len(await ClaimStore().recent(s, pid)) == 1  # the observer reads it
        await s.execute(text("DELETE FROM app.jmolt_claim"))
    async with scoped_session(maker, _admin(pid)) as s:
        assert await ClaimStore().recent(s, pid) == []
