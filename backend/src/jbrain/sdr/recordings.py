"""`app.sdr_recordings` read and written — the radio's library
(docs/plans/SDR_RECORDING_PLAN.md R1).

Shaped like `AprsReader`: the repo holds the session maker, and the RLS scope arrives
per call because it belongs to the turn rather than to the reader. The table is
`app.is_owner()`-gated with FORCE row-level security, so a non-owner gets an empty list
and a no-op DELETE from Postgres rather than a check here (CLAUDE.md #3).

**`blob_sha256` stays inside this module's callers and never inside a URL.** The audio
route resolves a recording's blob from the row it just read under the caller's scope —
serving `blobs.path_for(sha)` from a sha in the path would hand out any blob on the box
to anyone who could guess a digest, which is the firewall going around Postgres rather
than through it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

#: What a second of the sidecar's audio costs on disk: 64 kbps mono MP3 = 8 kB/s
#: (`deploy/sdr/listen.py` `AUDIO_BITRATE_BPS`). Used only to price what trimming has
#: given back — the recorded `bytes` of a live capture is counted, never estimated.
BITRATE_BPS = 64_000
BYTES_PER_S = BITRATE_BPS // 8

#: The library page. Newest first is the only order it is read in, and a radio library is
#: tens to hundreds of rows, not thousands — the ceiling exists so a client cannot ask
#: for the whole table plus every waveform in it in one response.
RECENT_DEFAULT = 100
RECENT_MAX = 500

# `peaks` is deliberately absent: the list draws a row per clip, and 400 floats per row
# would dwarf everything else in the response. The trim sheet fetches the one clip it is
# about, which is where the envelope is worth its bytes.
_LIST_COLUMNS = (
    "id::text AS id, started_at, ended_at, duration_s, captured_s, frequency_hz, mode,"
    " bandwidth_hz, gain, serial, bytes, transcribed_at,"
    " (transcript IS NOT NULL) AS has_transcript"
)
_ONE_COLUMNS = f"{_LIST_COLUMNS}, peaks, transcript, blob_sha256"


class RecordingsRepo:
    """The recordings library, on the caller's RLS scope."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker

    async def add(
        self,
        ctx: SessionContext,
        *,
        started_at: datetime,
        ended_at: datetime,
        duration_s: float,
        frequency_hz: int,
        mode: str,
        bandwidth_hz: int | None,
        gain: str | None,
        serial: str | None,
        blob_sha256: str,
        bytes_: int,
        peaks: list[float],
    ) -> dict[str, Any]:
        """Write one finished recording and return the row the library will show.

        `captured_s` is set from `duration_s` here and never again: it is what was
        originally recorded, and a later trim moves only `duration_s`. That is what makes
        "trimmed" a fact derived from the two numbers rather than a flag that can drift.
        """
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO app.sdr_recordings"
                        " (started_at, ended_at, duration_s, captured_s, frequency_hz, mode,"
                        " bandwidth_hz, gain, serial, blob_sha256, bytes, peaks)"
                        " VALUES (:started_at, :ended_at, :duration_s, :duration_s, :hz, :mode,"
                        " :bandwidth_hz, :gain, :serial, :sha, :bytes, CAST(:peaks AS jsonb))"
                        f" RETURNING {_ONE_COLUMNS}"
                    ),
                    {
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_s": duration_s,
                        "hz": frequency_hz,
                        "mode": mode,
                        "bandwidth_hz": bandwidth_hz,
                        "gain": gain,
                        "serial": serial,
                        "sha": blob_sha256,
                        "bytes": bytes_,
                        "peaks": json.dumps(peaks),
                    },
                )
            ).mappings()
            saved = dict(row.one())
            await session.commit()
            return saved

    async def recent(
        self, ctx: SessionContext, *, limit: int = RECENT_DEFAULT
    ) -> list[dict[str, Any]]:
        """The library, newest first."""
        bounded = max(1, min(int(limit), RECENT_MAX))
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {_LIST_COLUMNS} FROM app.sdr_recordings"
                        " ORDER BY started_at DESC LIMIT :limit"
                    ),
                    {"limit": bounded},
                )
            ).mappings()
            return [dict(row) for row in rows]

    async def get(self, ctx: SessionContext, recording_id: str) -> dict[str, Any] | None:
        """One recording, waveform and blob address included, or None.

        None covers both "no such row" and "not yours": RLS answers a non-owner with an
        empty result, and this reports that as a 404 rather than a 403 — the row's
        existence is not something a caller who cannot see it should learn.
        """
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(f"SELECT {_ONE_COLUMNS} FROM app.sdr_recordings WHERE id = :id"),
                    {"id": recording_id},
                )
            ).mappings()
            found = row.one_or_none()
            return dict(found) if found is not None else None

    async def usage(self, ctx: SessionContext) -> dict[str, int]:
        """`{count, bytes, reclaimed_bytes}` — what the library's disk line reports.

        `reclaimed_bytes` is priced from the seconds trimming has removed
        (`captured_s - duration_s`) rather than remembered, because nothing stores what a
        deleted blob weighed. It is the number the mock's "1.1 MB reclaimed by trimming"
        line shows, and at a constant 64 kbps it is exact to within a frame.
        """
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                text(
                    "SELECT count(*) AS count, COALESCE(sum(bytes), 0) AS bytes,"
                    " COALESCE(sum(GREATEST(captured_s - duration_s, 0)), 0) AS trimmed_s"
                    " FROM app.sdr_recordings"
                )
            )
            row = result.mappings().one()
            return {
                "count": int(row["count"]),
                "bytes": int(row["bytes"]),
                "reclaimed_bytes": int(float(row["trimmed_s"]) * BYTES_PER_S),
            }

    async def retrim(
        self,
        ctx: SessionContext,
        recording_id: str,
        *,
        duration_s: float,
        blob_sha256: str,
        bytes_: int,
        peaks: list[float],
    ) -> dict[str, Any] | None:
        """Repoint a row at its trimmed audio. `captured_s` is untouched, on purpose."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE app.sdr_recordings SET duration_s = :duration_s,"
                        " blob_sha256 = :sha, bytes = :bytes, peaks = CAST(:peaks AS jsonb)"
                        f" WHERE id = :id RETURNING {_ONE_COLUMNS}"
                    ),
                    {
                        "id": recording_id,
                        "duration_s": duration_s,
                        "sha": blob_sha256,
                        "bytes": bytes_,
                        "peaks": json.dumps(peaks),
                    },
                )
            ).mappings()
            updated = row.one_or_none()
            await session.commit()
            return dict(updated) if updated is not None else None

    async def remove(self, ctx: SessionContext, recording_id: str) -> str | None:
        """Delete a row, returning the blob it held so the caller can free it.

        Returning the sha rather than deleting the blob here keeps this module free of
        file I/O — and the caller has to ask `blob_in_use` before unlinking anyway.
        """
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text("DELETE FROM app.sdr_recordings WHERE id = :id RETURNING blob_sha256"),
                    {"id": recording_id},
                )
            ).mappings()
            gone = row.one_or_none()
            await session.commit()
            return cast(str, gone["blob_sha256"]) if gone is not None else None

    async def blob_in_use(
        self, ctx: SessionContext, sha256: str, *, except_id: str | None = None
    ) -> bool:
        """Whether any recording other than `except_id` still points at this blob.

        Blobs are content-addressed, so two rows with identical bytes are one file — and
        the reachable case is not a coincidence but a trim of the whole clip, which
        re-`put`s bytes identical to the original and gets the SAME digest back. Deleting
        "the old blob" there would delete the audio the row was just repointed at.
        """
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        # CAST because an untyped bind in `:x IS NULL` is a parameter
                        # whose type Postgres cannot infer, and asyncpg prepares.
                        "SELECT 1 FROM app.sdr_recordings WHERE blob_sha256 = :sha"
                        " AND (CAST(:except_id AS uuid) IS NULL"
                        " OR id <> CAST(:except_id AS uuid)) LIMIT 1"
                    ),
                    {"sha": sha256, "except_id": except_id},
                )
            ).first()
            return row is not None
