"""The worker-side drain for the Gmail metadata backfill (docs/EMAIL_ARCHIVIST_PLAN.md
Wave F).

The backfill is an hour-plus scan (Gmail's ~50 gets/sec quota), so it runs as its own
loop alongside the worker's job loop — never blocking note ingestion. It is resumable:
all state lives in `gmail_index_state` + the per-row `state`, so a restart picks up
exactly where it stopped. Owner-only RLS is principal-kind based, so one owner context
reaches every owner's index rows; each is driven by its stored principal_id.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.gmail.client import GmailError
from jbrain.gmail.indexer import GmailIndexer
from jbrain.gmail.provider import GmailClientProvider
from jbrain.models.gmail_index import GmailIndexStateRepo

log = structlog.get_logger()

_OWNER = SessionContext(principal_kind="owner")
# When no index is mid-build, re-check this often; while draining, loop with no sleep so
# the scan runs at Gmail's quota ceiling rather than a fixed tick.
_IDLE_SECONDS = 15.0


async def _next_active_principal(maker: async_sessionmaker) -> str | None:
    async with scoped_session(maker, _OWNER) as session:
        row = (
            await session.execute(
                text(
                    "SELECT principal_id FROM app.gmail_index_state"
                    " WHERE enabled AND phase IN ('discovering', 'fetching') LIMIT 1"
                )
            )
        ).first()
    return row[0] if row else None


async def drain_once(
    maker: async_sessionmaker, provider: GmailClientProvider, indexer: GmailIndexer
) -> bool:
    """Advance one active index by a single bounded step. Returns True when there was work
    (caller loops immediately), False when nothing is building (caller sleeps)."""
    pid = await _next_active_principal(maker)
    if pid is None:
        return False
    try:
        client = await provider.client()
    except GmailError as exc:
        # Credentials gone/invalid: surface it on the index so the panel shows why, and
        # stop spinning until the owner reconnects + restarts the build.
        async with scoped_session(maker, _OWNER) as session:
            await GmailIndexStateRepo().upsert(session, pid, phase="error", error=str(exc)[:500])
        log.warning("gmail.drain_no_client", error=repr(exc))
        return False
    async with scoped_session(maker, _OWNER) as session:
        await indexer.step(session, pid, client)
    return True


async def gmail_drain_loop(maker: async_sessionmaker, provider: GmailClientProvider) -> None:
    """Continuously drain enabled Gmail indexes, independent of the job loop. Survives DB/
    Gmail blips like the job loop does; cancellation propagates for clean shutdown."""
    indexer = GmailIndexer()
    while True:
        try:
            did_work = await drain_once(maker, provider, indexer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a transient blip must not kill the loop
            log.warning("gmail.drain_error", error=repr(exc))
            did_work = False
        await asyncio.sleep(0 if did_work else _IDLE_SECONDS)
