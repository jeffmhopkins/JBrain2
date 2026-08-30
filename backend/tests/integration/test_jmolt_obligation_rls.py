"""Migration 0182 against real Postgres: jmolt's obligation ledger.

Two things are being pinned. The RLS split — jmolt opens, evidences and discharges its own
rows and can never delete one, an outsider sees nothing — and the ledger's actual semantics,
which are what the second engine's identity claim rests on: the agent is the sum of its
unfinished business, most recently disturbed first.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_obligation import ObligationRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


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


def _outsider() -> SessionContext:
    return SessionContext(principal_id="stranger", principal_kind="capability_token")


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    """Empty the ledger as the OWNER — jmolt cannot delete, which is the point of the table,
    so a test that tried to clean up as jmolt would silently leave every earlier row behind."""
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_obligation"))
        assert (await s.execute(text("SELECT count(*) FROM app.jmolt_obligation"))).scalar() == 0
    yield


# --- semantics --------------------------------------------------------------


async def test_opening_the_same_obligation_twice_touches_it_rather_than_duplicating(
    maker: async_sessionmaker,
) -> None:
    """Promise extraction runs every night over text the model may not remember writing, so
    "open this, again, possibly" is the only sane contract for that path."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        first = await repo.open(s, pid, kind="question", subject="whether verification differs")
        again = await repo.open(s, pid, kind="question", subject="whether verification differs")
        assert first == again
        assert (await repo.counts(s, pid)) == {"question": 1}


async def test_evidence_is_verbatim_and_de_duplicated(maker: async_sessionmaker) -> None:
    """Re-reading a thread twice in a night must not double the evidence — but it does touch
    the obligation, because encountering something again is a fact about what is live."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        oid = await repo.open(s, pid, kind="person", subject="@otheragent")
        assert oid is not None
        assert (
            await repo.evidence(
                s, pid, oid, quote="I think you are wrong about weeks.", source="c1"
            )
            is True
        )
        assert (
            await repo.evidence(
                s, pid, oid, quote="I think you are wrong about weeks.", source="c1"
            )
            is False
        )
        [row] = await repo.open_(s, pid)
        assert [e.quote for e in row.evidence] == ["I think you are wrong about weeks."]
        assert row.evidence[0].source == "c1"


async def test_the_brief_is_ordered_by_what_was_last_disturbed(maker: async_sessionmaker) -> None:
    """That ordering IS the identity claim: the agent is its unfinished business, most
    recently disturbed first — not the oldest thing it ever wondered."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        old = await repo.open(s, pid, kind="question", subject="an old wondering")
        await repo.open(s, pid, kind="question", subject="a newer wondering")
        assert [o.subject for o in await repo.open_(s, pid)][0] == "a newer wondering"
        assert old is not None
        await repo.touch(s, pid, old)
        assert [o.subject for o in await repo.open_(s, pid)][0] == "an old wondering"


async def test_re_encountering_something_closed_reopens_it(maker: async_sessionmaker) -> None:
    """A closed row that stays closed is how a promise someone chased up disappears."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        oid = await repo.open(s, pid, kind="commitment", subject="reply to @otheragent")
        assert oid is not None
        assert await repo.close(s, pid, oid, resolution="replied last night") is True
        assert await repo.open_(s, pid) == []
        assert await repo.open(s, pid, kind="commitment", subject="reply to @otheragent") == oid
        assert [o.subject for o in await repo.open_(s, pid)] == ["reply to @otheragent"]


async def test_closing_is_idempotent_and_abandoning_is_a_first_class_outcome(
    maker: async_sessionmaker,
) -> None:
    """An agent that can only ever discharge accumulates a brief it can never finish reading,
    and that pressure is indistinguishable from the pressure to post."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    since = datetime.now(UTC) - timedelta(minutes=1)
    async with scoped_session(maker, _jmolt(pid)) as s:
        oid = await repo.open(s, pid, kind="question", subject="something I lost interest in")
        assert oid is not None
        assert await repo.close(s, pid, oid, resolution="not mine", abandoned=True) is True
        assert await repo.close(s, pid, oid, resolution="again") is False  # already closed
        [closed] = await repo.closed_since(s, pid, since=since)
        assert closed.status == "abandoned" and closed.resolution == "not mine"


async def test_a_subject_that_is_prose_is_refused(maker: async_sessionmaker) -> None:
    """The subject is a HANDLE the composer prints, not the thing itself. A blank one prints
    as an empty line in tomorrow's brief; a paragraph is the prose this table replaces."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await repo.open(s, pid, kind="question", subject="   ") is None
        assert await repo.open(s, pid, kind="question", subject="x" * 500) is None
        assert await repo.open(s, pid, kind="nonsense", subject="fine") is None
        assert await repo.counts(s, pid) == {}


async def test_the_brief_is_bounded_in_both_directions(maker: async_sessionmaker) -> None:
    """An unbounded ledger reproduces the failure it replaces: a context that grows until the
    model reads its own past instead of the world."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        for i in range(20):
            oid = await repo.open(s, pid, kind="question", subject=f"question {i}")
            assert oid is not None
            for j in range(6):
                await repo.evidence(s, pid, oid, quote=f"quote {i}-{j}")
        rows = await repo.open_(s, pid, limit=5, evidence_each=2)
        assert len(rows) == 5
        assert all(len(r.evidence) == 2 for r in rows)


# --- RLS --------------------------------------------------------------------


async def test_jmolt_cannot_delete_its_own_history(maker: async_sessionmaker) -> None:
    """Same reason it cannot delete its action ledger: the record of what it owed is
    owner-side evidence about jmolt, not jmolt's to curate."""
    pid = await _owner_pid(maker)
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        await repo.open(s, pid, kind="question", subject="an inconvenient question")
    async with scoped_session(maker, _jmolt(pid)) as s:
        await s.execute(text("DELETE FROM app.jmolt_obligation"))
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert len(await repo.open_(s, pid)) == 1  # still there


async def test_jmolt_cannot_write_a_row_keyed_to_anyone_else(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    async with scoped_session(maker, _jmolt(pid)) as s:
        with pytest.raises(Exception):  # noqa: B017 — RLS refuses; the class is the driver's
            await ObligationRepo().open(s, "someone-else", kind="question", subject="theirs")


async def test_an_outsider_sees_nothing(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    async with scoped_session(maker, _jmolt(pid)) as s:
        await ObligationRepo().open(s, pid, kind="question", subject="private")
    async with scoped_session(maker, _outsider()) as s:
        assert await ObligationRepo().open_(s, pid) == []


async def test_the_owner_can_read_and_prune(maker: async_sessionmaker) -> None:
    """The observer and the digest read this as the owner; only the system prunes."""
    pid = await _owner_pid(maker)
    async with scoped_session(maker, _jmolt(pid)) as s:
        await ObligationRepo().open(s, pid, kind="question", subject="visible to my human")
    async with scoped_session(maker, _admin(pid)) as s:
        assert len(await ObligationRepo().open_(s, pid)) == 1
        await s.execute(text("DELETE FROM app.jmolt_obligation"))
    async with scoped_session(maker, _admin(pid)) as s:
        assert await ObligationRepo().open_(s, pid) == []
