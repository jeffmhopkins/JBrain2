"""Retention for voice post: played messages age out, unplayed ones never do.

`JPANEL_PLAN.md` §5 left this as two sentences — *"30 days after playing is a proposal.
Unplayed-forever is not."* — and the second half is the one that decides the shape of this
file. A message nobody has heard is a four-year-old's words sitting on a wall waiting for
somebody to come back to it, and the whole feature is built around the promise that it waits.
So the sweep's window is measured from `played_at`, which is NULL for exactly those messages;
an unplayed row is not old, whatever its `created_at` says, and no amount of time makes it
eligible.

Thirty days is the proposal and is still a guess — nobody has yet wanted a message back from
three weeks ago — so it is one named constant rather than a number spread through the SQL.

**The blob is not the row's to delete.** Audio is content-addressed (`CLAUDE.md` #2): two rows
with identical bytes are one file with one digest, and `blob_refs.BLOB_REFERENCES` is the list
that answers "does anything else still point at this". Dropping a message's file without asking
is how the owner's chat attachment of that same clip starts 500ing — which is not hypothetical,
it is the fault `blob_refs.py` was written for. So the rows go first and commit, and only then
is each digest offered for collection, against a schema that no longer counts the rows just
removed.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.blob_refs import blob_referenced
from jbrain.db.session import SessionContext, scoped_session
from jbrain.storage import BlobStore

log = structlog.get_logger(__name__)

#: The proposal from `JPANEL_PLAN.md` §5, measured from when a message was PLAYED.
RETAIN_PLAYED_DAYS = 30

#: Six hours. This deletes a handful of rows in a house with two panels, and a sweep that runs
#: more often than the thing it cleans up is just load with a log line attached.
SWEEP_INTERVAL_SECONDS = 6 * 60 * 60


async def sweep_played_messages(
    maker: async_sessionmaker,
    blobs: BlobStore,
    ctx: SessionContext,
    *,
    older_than_days: int = RETAIN_PLAYED_DAYS,
) -> tuple[int, int]:
    """Delete played messages past the window and collect any audio nothing else holds.

    Returns `(rows deleted, blobs unlinked)` — two numbers rather than one because they are
    routinely different and the difference is the interesting part: a digest still held by a
    chat attachment leaves a file behind on purpose.
    """
    async with scoped_session(maker, ctx) as session:
        gone = (
            await session.execute(
                text(
                    """
                    DELETE FROM app.jpanel_message
                    WHERE played_at IS NOT NULL
                      AND played_at < now() - make_interval(days => :days)
                    RETURNING blob_sha256
                    """
                ),
                {"days": older_than_days},
            )
        ).all()
        await session.commit()

    rows = len(gone)
    if rows == 0:
        return (0, 0)

    # Distinct, because two messages CAN be one file: the owner sending the same words twice
    # produces identical TTS bytes and therefore identical digests, and asking twice would
    # report a phantom second collection.
    shas = {str(r[0]) for r in gone if r[0]}
    freed = 0
    for sha in shas:
        # A fresh session per digest, after the commit above, so the rows just deleted are not
        # counted as holders of their own blob.
        async with scoped_session(maker, ctx) as session:
            if await blob_referenced(session, sha):
                continue
        if await blobs.delete(sha):
            freed += 1

    log.info("jpanel.retention.swept", deleted=rows, blobs_freed=freed, days=older_than_days)
    return (rows, freed)


async def jpanel_retention_loop(
    maker: async_sessionmaker,
    blobs: BlobStore,
    ctx: SessionContext,
    *,
    interval_seconds: int = SWEEP_INTERVAL_SECONDS,
    older_than_days: int = RETAIN_PLAYED_DAYS,
) -> None:
    """Sweep forever, sleeping between passes.

    A failed sweep is logged and the loop continues — a transient database hiccup must not
    leave retention switched off until the next deploy — and cancellation propagates so
    shutdown stops it cleanly. The sibling of `intake.sweep`'s reaper, and wired the same way.
    """
    while True:
        try:
            await sweep_played_messages(maker, blobs, ctx, older_than_days=older_than_days)
        except Exception:
            log.exception("jpanel.retention.failed")
        await asyncio.sleep(interval_seconds)
