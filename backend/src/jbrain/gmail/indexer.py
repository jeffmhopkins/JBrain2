"""The Gmail metadata backfill (docs/EMAIL_ARCHIVIST_PLAN.md).

A resumable, two-phase sync that fills `gmail_message_meta`: **discover** pages every
message id (cheap — ids only) into `pending` rows, then **fetch** each id's metadata
(`From`/`Date`/`labels`, never the body) in bounded concurrent batches and marks it
`done`. Each `step()` does one bounded unit of work and the driver commits after it, so
the scan checkpoints in the DB (the `discovery_cursor` page token + the per-row `state`)
and resumes exactly where it stopped after a restart — no work is redone.

Throughput is capped by Gmail's quota (~50 metadata gets/sec), so a large mailbox is an
hour-plus job; that is why it runs on the background worker, not a request.
"""

from __future__ import annotations

import asyncio
from email.utils import parseaddr

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.gmail.client import GmailApi, GmailError
from jbrain.models.gmail_index import (
    GmailIndexStateRepo,
    GmailMetaRepo,
    IndexProgress,
    epoch_ms_to_dt,
)

# Discovery covers the WHOLE mailbox (All Mail), not just the inbox, so the index can
# answer all-time questions. Bounds keep a single step short enough to checkpoint.
_DISCOVER_QUERY = "in:anywhere"
_FETCH_BATCH = 50
_FETCH_CONCURRENCY = 10


class GmailIndexer:
    """Drives the backfill over a caller-supplied RLS-scoped session. The caller (the
    background worker, or a test) loops `step()` and commits after each, until the
    returned progress reports phase `ready`."""

    def __init__(
        self,
        meta: GmailMetaRepo | None = None,
        state: GmailIndexStateRepo | None = None,
        *,
        discover_query: str = _DISCOVER_QUERY,
        fetch_batch: int = _FETCH_BATCH,
    ):
        self.meta = meta or GmailMetaRepo()
        self.state = state or GmailIndexStateRepo()
        self._discover_query = discover_query
        self._fetch_batch = fetch_batch

    async def begin(
        self, session: AsyncSession, principal_id: str, client: GmailApi, *, reset: bool = False
    ) -> IndexProgress:
        """Start (or restart) the backfill: stamp the mailbox size + history cursor and
        move to the discovery phase. `reset=True` clears this principal's rows for a true
        rebuild; otherwise existing `done` rows are kept and only new ids are added."""
        if reset:
            await self.meta.clear(session, principal_id)
        total, history_id = await client.get_profile()
        await self.state.upsert(
            session,
            principal_id,
            enabled=True,
            phase="discovering",
            total_estimate=total,
            last_history_id=history_id or None,
            discovery_cursor=None,
            started_at=func.now(),
            error=None,
        )
        return await self.state.progress(session, principal_id, self.meta)

    async def step(
        self, session: AsyncSession, principal_id: str, client: GmailApi
    ) -> IndexProgress:
        """One bounded unit of work for an enabled backfill: a discovery page, or a fetch
        batch. A no-op (returns current progress) when disabled or already `ready`."""
        state = await self.state.get(session, principal_id)
        if state is None or not state.enabled or state.phase in ("ready", "idle", "error"):
            return await self.state.progress(session, principal_id, self.meta)
        try:
            if state.phase == "discovering":
                await self._discover(session, principal_id, client, state.discovery_cursor)
            elif state.phase == "fetching":
                await self._fetch(session, principal_id, client)
        except GmailError as exc:
            await self.state.upsert(session, principal_id, phase="error", error=str(exc)[:500])
        return await self.state.progress(session, principal_id, self.meta)

    async def _discover(
        self, session: AsyncSession, principal_id: str, client: GmailApi, cursor: str | None
    ) -> None:
        ids, next_cursor = await client.list_page(self._discover_query, page_token=cursor)
        await self.meta.upsert_discovered(session, principal_id, [(gid, "") for gid in ids])
        await self.state.upsert(
            session,
            principal_id,
            discovery_cursor=next_cursor,
            phase="discovering" if next_cursor else "fetching",
        )

    async def _fetch(self, session: AsyncSession, principal_id: str, client: GmailApi) -> None:
        gids = await self.meta.claim_pending(session, principal_id, limit=self._fetch_batch)
        if not gids:
            await self.state.upsert(session, principal_id, phase="ready")
            return
        # Fetch the batch's metadata concurrently (HTTP, no DB), then write the results
        # to the single session sequentially — one session is not concurrency-safe.
        for chunk_start in range(0, len(gids), _FETCH_CONCURRENCY):
            chunk = gids[chunk_start : chunk_start + _FETCH_CONCURRENCY]
            results = await asyncio.gather(
                *(client.get(gid, metadata_only=True) for gid in chunk),
                return_exceptions=True,
            )
            for gid, result in zip(chunk, results, strict=True):
                if isinstance(result, BaseException):
                    await self.meta.mark_error(session, principal_id, gid, str(result))
                    continue
                await self.meta.save_meta(
                    session,
                    principal_id,
                    gid,
                    sender_email=parseaddr(result.sender)[1].lower(),
                    subject=result.subject,
                    sent_at=epoch_ms_to_dt(result.internal_date_ms),
                    label_ids=list(result.label_ids),
                )
