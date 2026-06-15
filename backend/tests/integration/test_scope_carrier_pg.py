"""The E1 scope carrier against real Postgres: the no-confused-deputy property.

Proves end-to-end (migration 0039 + queue + worker + db.session, all on real RLS)
that:
- a stamped job's NARROWED execution context cannot read or write another domain's
  rows — Postgres RLS, not application politeness, denies it (CLAUDE.md rule 3,
  docs/WORKFLOW_ENGINE_PLAN.md §2 E1, ASSISTANT.md I-8);
- an UNSTAMPED job (every job today, the six shipped kinds) still runs under the
  all-domains SYSTEM_CTX exactly as before — no regression;
- a PARTIAL stamp fails closed: the job is failed without ever running, never
  silently widened to system.

The stamp lives on app.jobs, which stays owner-only; the firewall the narrowed
context hits is on app.chunks (the standard has_domain_scope domain policy).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import queue, worker
from jbrain.db.session import ScopeStampError, scoped_session
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed_chunk(maker: async_sessionmaker, domain: str) -> tuple[str, str]:
    """A note + one chunk in `domain`; returns (note_id, chunk_id). The chunk is the
    domain-firewalled row the narrowed context is tested against."""
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"sc-{uuid.uuid4().hex[:10]}", domain=domain, destination=None, body="x"
    )
    chunk_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :nid, :dom, 'paragraph', 0, 'c')"
            ),
            {"id": chunk_id, "nid": note.id, "dom": domain},
        )
    return note.id, chunk_id


async def count_chunks(maker: async_sessionmaker, ctx: Any, chunk_id: str) -> int:
    async with scoped_session(maker, ctx) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.chunks WHERE id = :id"), {"id": chunk_id}
            )
        ).scalar_one()


async def test_stamp_round_trips_through_enqueue_and_claim(
    maker: async_sessionmaker,
) -> None:
    """enqueue stores the (principal_id, domain_code) stamp; claim returns it."""
    await quiesce(maker)
    principal = str(uuid.uuid4())
    job_id = await queue.enqueue(
        maker,
        OWNER,
        "integrate_note",
        {"note_id": "n"},
        principal_id=principal,
        domain_code="health",
    )
    claimed = await queue.claim(maker, queue.SYSTEM_CTX)
    assert claimed is not None and claimed.id == job_id
    assert claimed.principal_id == principal
    assert claimed.domain_code == "health"
    assert claimed.is_stamped is True
    await queue.complete(maker, queue.SYSTEM_CTX, job_id)


async def test_unstamped_job_claims_as_system(maker: async_sessionmaker) -> None:
    """A job enqueued without a stamp (every caller today) carries no scope and
    resolves to SYSTEM_CTX — the regression guard for the six shipped kinds."""
    await quiesce(maker)
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "n"})
    claimed = await queue.claim(maker, queue.SYSTEM_CTX)
    assert claimed is not None and claimed.id == job_id
    assert claimed.principal_id is None and claimed.domain_code is None
    assert claimed.is_stamped is False
    assert worker.resolve_exec_context(claimed) is queue.SYSTEM_CTX
    await queue.complete(maker, queue.SYSTEM_CTX, job_id)


async def test_narrowed_context_cannot_cross_the_firewall(maker: async_sessionmaker) -> None:
    """THE no-confused-deputy proof: a health-stamped job's narrowed context reads
    its own health chunk but is RLS-denied another domain's (finance) — and cannot
    write a finance chunk either. Postgres enforces it, not the application."""
    await quiesce(maker)
    _, health_chunk = await seed_chunk(maker, "health")
    _, finance_chunk = await seed_chunk(maker, "finance")

    principal = str(uuid.uuid4())
    job_id = await queue.enqueue(
        maker,
        OWNER,
        "integrate_note",
        {"note_id": "n"},
        principal_id=principal,
        domain_code="health",
    )
    claimed = await queue.claim(maker, queue.SYSTEM_CTX)
    assert claimed is not None
    exec_ctx = worker.resolve_exec_context(claimed)
    assert exec_ctx is not queue.SYSTEM_CTX and exec_ctx.owner_scoped is True

    # Reads: its own domain is visible, the other domain is invisible (RLS filters).
    assert await count_chunks(maker, exec_ctx, health_chunk) == 1
    assert await count_chunks(maker, exec_ctx, finance_chunk) == 0

    # Writes: it cannot stamp a finance chunk — the WITH CHECK firewall refuses it.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, exec_ctx) as s:
            await s.execute(
                text(
                    "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                    " SELECT gen_random_uuid(), note_id, 'finance', 'paragraph', 1, 'sneak'"
                    " FROM app.chunks WHERE id = :id"
                ),
                {"id": health_chunk},
            )
    await queue.complete(maker, queue.SYSTEM_CTX, job_id)


async def test_system_job_sees_every_domain(maker: async_sessionmaker) -> None:
    """The contrast case: an unstamped (system) job's context crosses every domain,
    exactly as the worker does today — the cross-domain pipelines are not broken."""
    await quiesce(maker)
    _, health_chunk = await seed_chunk(maker, "health")
    _, finance_chunk = await seed_chunk(maker, "finance")

    job_id = await queue.enqueue(maker, OWNER, "consolidate_predicates", {})
    claimed = await queue.claim(maker, queue.SYSTEM_CTX)
    assert claimed is not None
    exec_ctx = worker.resolve_exec_context(claimed)
    assert exec_ctx is queue.SYSTEM_CTX
    assert await count_chunks(maker, exec_ctx, health_chunk) == 1
    assert await count_chunks(maker, exec_ctx, finance_chunk) == 1
    await queue.complete(maker, queue.SYSTEM_CTX, job_id)


async def test_partial_stamp_fails_closed_end_to_end(maker: async_sessionmaker) -> None:
    """A job persisted with a principal but no domain claims as a partial stamp;
    resolving its scope raises (fail-closed), and process_one fails it permanently
    WITHOUT running any handler — no silent escalation to SYSTEM_CTX."""
    await quiesce(maker)
    principal = str(uuid.uuid4())
    # Insert the half-stamp directly: enqueue's contract is both-or-neither, but a
    # forged/buggy row must still be rejected at the worker, so test that row.
    job_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload, principal_id)"
                " VALUES (:id, 'ingest_note', '{}'::jsonb, :pid)"
            ),
            {"id": job_id, "pid": principal},
        )

    claimed = await queue.claim(maker, queue.SYSTEM_CTX)
    assert claimed is not None and claimed.id == job_id
    assert claimed.is_stamped is True  # a half-stamp is still "stamped"
    with pytest.raises(ScopeStampError):
        worker.resolve_exec_context(claimed)

    # The full worker path: the handler must never run, and the job fails for good.
    ran = False

    async def handler(_payload: dict[str, Any]) -> None:
        nonlocal ran
        ran = True

    # Re-queue the claimed job so process_one can claim it afresh.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.jobs SET status = 'queued', locked_at = NULL WHERE id = :id"),
            {"id": job_id},
        )
    assert await worker.process_one(maker, {"ingest_note": handler}) is True
    assert ran is False
    async with scoped_session(maker, OWNER) as s:
        status = (
            await s.execute(text("SELECT status FROM app.jobs WHERE id = :id"), {"id": job_id})
        ).scalar_one()
    assert status == "failed"


async def quiesce(maker: async_sessionmaker) -> None:
    """Park jobs other tests left so claim() sees only ours."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.jobs SET status = 'done' WHERE status != 'done'"))
