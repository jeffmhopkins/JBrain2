"""Migration 0096 against real Postgres: the Gmail metadata index + its state row are
owner-only (CLAUDE.md rule 3), and the resumable backfill drains a FakeGmail mailbox into
exact, queryable sender/day aggregates — the analytics Gmail's API can't do itself.
"""

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api.gmail_settings import read_gmail_index, start_gmail_index
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.gmail import FakeGmail, GmailError
from jbrain.gmail.client import GmailApi, GmailMessage
from jbrain.gmail.drain import drain_once
from jbrain.gmail.indexer import GmailIndexer
from jbrain.models.gmail_index import GmailIndexStateRepo, GmailMetaRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# 2026-01-02 and 2026-01-03 in epoch ms — two distinct UTC days for the histogram test.
_DAY1_MS = 1_767_312_000_000
_DAY2_MS = 1_767_398_400_000


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _owner() -> SessionContext:
    """A fresh owner context per test. is_owner() keys only on the principal_kind GUC, so
    a unique principal_id fully isolates each test's rows in the module-shared database."""
    return SessionContext(principal_id=str(uuid.uuid4()), principal_kind="owner")


def _msg(mid: str, sender: str, ms: int) -> GmailMessage:
    return GmailMessage(
        id=mid,
        thread_id=f"t{mid}",
        sender=sender,
        to="me@x.com",
        subject="s",
        date="d",
        snippet="",
        body="body",
        label_ids=("INBOX",),
        internal_date_ms=ms,
    )


def _mailbox() -> FakeGmail:
    # 3 chase, 2 amazon, 1 personal; two on day1, the rest on day2.
    return FakeGmail(
        [
            _msg("m1", "Chase <a@chase.com>", _DAY1_MS),
            _msg("m2", "b@chase.com", _DAY1_MS),
            _msg("m3", "c@chase.com", _DAY2_MS),
            _msg("m4", "ship@amazon.com", _DAY2_MS),
            _msg("m5", "deals@amazon.com", _DAY2_MS),
            _msg("m6", "mom@family.net", _DAY2_MS),
        ]
    )


async def _drain(maker: async_sessionmaker, owner: SessionContext, fake: FakeGmail) -> None:
    indexer = GmailIndexer(fetch_batch=2)  # small batch so several steps are needed
    async with scoped_session(maker, owner) as session:
        await indexer.begin(session, owner.principal_id, fake)
    for _ in range(50):  # generous bound; the mailbox is tiny
        async with scoped_session(maker, owner) as session:
            progress = await indexer.step(session, owner.principal_id, fake)
        if progress.phase == "ready":
            return
    raise AssertionError("backfill did not reach 'ready'")


async def test_index_tables_are_owner_only(maker: async_sessionmaker) -> None:
    owner = _owner()
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.gmail_message_meta (principal_id, gmail_id, state, sender_domain)"
                " VALUES (:pid, 'g1', 'done', 'chase.com')"
            ),
            {"pid": owner.principal_id},
        )
        await session.execute(
            text("INSERT INTO app.gmail_index_state (principal_id, phase) VALUES (:pid, 'ready')"),
            {"pid": owner.principal_id},
        )
    # A non-owner principal sees neither table's rows (RLS).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.gmail_message_meta"))
        ).scalar() == 0
        assert (
            await session.execute(text("SELECT count(*) FROM app.gmail_index_state"))
        ).scalar() == 0


async def test_backfill_drains_to_ready_and_indexes_every_message(
    maker: async_sessionmaker,
) -> None:
    owner = _owner()
    await _drain(maker, owner, _mailbox())
    meta = GmailMetaRepo()
    async with scoped_session(maker, owner) as session:
        progress = await GmailIndexStateRepo().progress(session, owner.principal_id, meta)
    assert progress.phase == "ready"
    assert progress.indexed == 6
    assert progress.pending == 0
    assert progress.total_estimate == 6  # FakeGmail.get_profile reports the mailbox size


async def test_top_senders_are_exact_and_ranked(maker: async_sessionmaker) -> None:
    owner = _owner()
    await _drain(maker, owner, _mailbox())
    meta = GmailMetaRepo()
    async with scoped_session(maker, owner) as session:
        by_domain = await meta.top_senders(session, owner.principal_id, by="domain", limit=10)
        by_address = await meta.top_senders(session, owner.principal_id, by="address", limit=10)
    assert by_domain[:3] == [("chase.com", 3), ("amazon.com", 2), ("family.net", 1)]
    # Exact per-address counts too (a@chase.com etc. each appear once).
    assert ("a@chase.com", 1) in by_address


async def test_volume_by_day_buckets_by_date(maker: async_sessionmaker) -> None:
    owner = _owner()
    await _drain(maker, owner, _mailbox())
    meta = GmailMetaRepo()
    async with scoped_session(maker, owner) as session:
        days = await meta.volume_by_day(session, owner.principal_id)
    counts = [n for _, n in days]
    assert counts == [2, 4]  # 2 on day1, 4 on day2, ordered by day


class _FakeProvider:
    """Stands in for GmailClientProvider — hands the worker drain / API a FakeGmail (or
    raises GmailError to simulate unconfigured credentials)."""

    def __init__(self, client: GmailApi | None):
        self._client = client

    async def client(self) -> GmailApi:
        if self._client is None:
            raise GmailError("Gmail is not configured on this instance")
        return self._client


async def test_drain_loop_indexes_via_provider(maker: async_sessionmaker) -> None:
    """The worker's drain_once advances an enabled index to ready, pulling its client from
    the provider and keying off the stored principal_id under an owner-kind session."""
    owner = _owner()
    fake = _mailbox()
    provider = _FakeProvider(fake)
    indexer = GmailIndexer(fetch_batch=2)
    async with scoped_session(maker, owner) as session:
        await indexer.begin(session, owner.principal_id, fake)
    for _ in range(50):
        if not await drain_once(maker, provider, indexer):  # type: ignore[arg-type]
            break
    async with scoped_session(maker, owner) as session:
        progress = await GmailIndexStateRepo().progress(
            session, owner.principal_id, GmailMetaRepo()
        )
    assert progress.phase == "ready"
    assert progress.indexed == 6


async def test_drain_marks_error_when_gmail_unconfigured(maker: async_sessionmaker) -> None:
    owner = _owner()
    indexer = GmailIndexer()
    # Enable the index pointed at a working client, then drain with creds revoked.
    async with scoped_session(maker, owner) as session:
        await indexer.begin(session, owner.principal_id, _mailbox())
    did_work = await drain_once(maker, _FakeProvider(None), indexer)  # type: ignore[arg-type]
    assert did_work is False
    async with scoped_session(maker, owner) as session:
        progress = await GmailIndexStateRepo().progress(
            session, owner.principal_id, GmailMetaRepo()
        )
    assert progress.phase == "error"
    assert progress.error


def _req(maker: async_sessionmaker, provider: object) -> object:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=maker, gmail_provider=provider))
    )


def _principal(owner: SessionContext) -> PrincipalInfo:
    return PrincipalInfo(id=owner.principal_id or "", kind="owner", label="t")


async def test_index_endpoints_start_and_report_status(maker: async_sessionmaker) -> None:
    owner = _owner()
    principal = _principal(owner)
    request = _req(maker, _FakeProvider(_mailbox()))

    before = await read_gmail_index(request, principal)  # type: ignore[arg-type]
    assert before.phase == "idle" and before.enabled is False

    started = await start_gmail_index(SimpleNamespace(rebuild=False), request, principal)  # type: ignore[arg-type]
    assert started.enabled is True
    assert started.phase == "discovering"
    assert started.total == 6  # get_profile reported the mailbox size → progress denominator


async def test_start_index_requires_connected_gmail(maker: async_sessionmaker) -> None:
    from fastapi import HTTPException

    owner = _owner()
    request = _req(maker, _FakeProvider(None))  # creds not configured
    with pytest.raises(HTTPException) as exc:
        await start_gmail_index(SimpleNamespace(rebuild=False), request, _principal(owner))  # type: ignore[arg-type]
    assert exc.value.status_code == 400


async def test_backfill_is_resumable_midway(maker: async_sessionmaker) -> None:
    """After a few steps the index is partial (some pending); continuing finishes it
    without redoing work — the checkpoint is the per-row state + the discovery cursor."""
    owner = _owner()
    fake = _mailbox()
    indexer = GmailIndexer(fetch_batch=2)
    async with scoped_session(maker, owner) as session:
        await indexer.begin(session, owner.principal_id, fake)
    # One discovery step + one fetch batch → some done, some still pending.
    async with scoped_session(maker, owner) as session:
        await indexer.step(session, owner.principal_id, fake)  # discover
        await indexer.step(session, owner.principal_id, fake)  # fetch batch of 2
        mid = await GmailIndexStateRepo().progress(session, owner.principal_id, indexer.meta)
    assert 0 < mid.indexed < 6
    assert mid.pending > 0
    # Resume to completion.
    await _drain(maker, owner, fake)
    async with scoped_session(maker, owner) as session:
        done = await GmailIndexStateRepo().progress(session, owner.principal_id, indexer.meta)
    assert done.indexed == 6 and done.pending == 0
