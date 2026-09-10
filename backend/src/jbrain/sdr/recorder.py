"""Recording the live radio: one more subscriber on the sidecar's audio stream.

docs/plans/SDR_RECORDING_PLAN.md R1. The api records rather than the sidecar because
`deploy/docker-compose.yml` gives the `sdr` service no volume and 512 MB — anything it
wrote would die on the next Ops → Update, and it cannot hold the owner's data under RLS
either. So this opens its own `GET /listen/audio` (the same fan-out the browser's
`<audio>` uses, so recording never interrupts listening) and spools the bytes straight
into `blobs.put_stream(...)`: a clip is never buffered whole in memory, whatever its
length, and it lands through the storage abstraction as CLAUDE.md #2 requires.

**One recording at a time.** Not a limitation to work around: there is one listen
session, and a second recorder would spool a byte-identical second copy of it — which
content-addressing would then dedupe into one blob with two rows pointing at it, so
deleting either would take the other's audio. The single active slot is what makes
`blob_sha256` a one-to-one thing.

**An interrupted recording is still a recording.** The sidecar ending a session, a
dropped connection, or a retune that outlives the stream all close it from the far end,
and each of those must finalize the blob and write its row rather than discard what was
captured. That is why `_chunks` RETURNS on every ending — a raised exception would
propagate out of `put_stream`, unlink the spool file, and lose the audio.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from jbrain.db.session import SessionContext
from jbrain.sdr.audio import levels
from jbrain.sdr.recordings import RecordingsRepo
from jbrain.storage import BlobStore

log = structlog.get_logger(__name__)

#: Connect promptly, then read for as long as the owner records. `timeout=None` outright
#: would also make a sidecar that never accepts the connection hang the Record button
#: with nothing to say.
CONNECT_TIMEOUT_S = 10.0


class RecorderRefused(RuntimeError):
    """The sidecar would not open the stream, carrying its own sentence and status.

    Its status travels so the route can map it the way `api/sdr.py` maps every other
    sidecar answer — 409 "nothing is listening" is the owner-fixable one, and burying it
    in a 502 would tell them the box is broken when the fix is to press Listen.
    """

    def __init__(self, detail: str, *, status: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


async def open_audio_stream(base_url: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open the sidecar's live MP3, returning the client so the caller can close both.

    A module function so a test can replace it: everything below is about what happens
    to the bytes, and none of it should need a socket to prove.
    """
    client = httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S)
    )
    try:
        request = client.build_request("GET", "/listen/audio")
        return client, await client.send(request, stream=True)
    except BaseException:
        await client.aclose()
        raise


@dataclass
class _Active:
    """The recording in progress — everything the row will need, plus the stop signal."""

    ctx: SessionContext
    started_at: datetime
    frequency_hz: int
    mode: str
    bandwidth_hz: int | None
    gain: str | None
    serial: str | None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    bytes: int = 0
    task: asyncio.Task[None] | None = None


class SdrRecorder:
    """Records the live listen session to a blob, then to a row. At most one at a time."""

    def __init__(self, blobs: BlobStore, repo: RecordingsRepo) -> None:
        self._blobs = blobs
        self._repo = repo
        self._active: _Active | None = None
        # The row a recording that ended on its OWN wrote — the sidecar dropped the
        # session while the owner was still holding Record. Held so the next stop can
        # hand it back rather than answering "nothing was recording" about audio that is
        # sitting in the library.
        self._saved: dict[str, Any] | None = None
        # The finalize, kept past the end of the recording it belongs to. `_active` is
        # cleared the moment the stream ends, so a stop arriving while the row is still
        # being written would otherwise find nothing to wait for and report that nothing
        # was saved — about a clip that is landing in the library as it answers.
        self._task: asyncio.Task[None] | None = None

    def state(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """What `GET /sdr/status` reports, or None when nothing is recording.

        Elapsed and size come from here so the tape deck draws them off the 1 Hz poll
        every other radio field already uses, rather than running a second timer that
        keeps counting after the recording has stopped.
        """
        active = self._active
        return None if active is None else _state_of(active, now or datetime.now(tz=UTC))

    async def start(
        self,
        ctx: SessionContext,
        *,
        base_url: str,
        frequency_hz: int,
        mode: str,
        bandwidth_hz: int | None = None,
        gain: str | None = None,
        serial: str | None = None,
    ) -> dict[str, Any]:
        """Begin recording, or return the recording already running.

        Idempotent at the source rather than only in the route: two Record presses a
        moment apart are two requests that both saw "not recording", and the second must
        not open a second stream.

        The settings stored are the ones in force NOW. A retune does not restart the
        pipeline, so a recording can span a frequency change; the row keeps where it
        started, which is what the library shows (the plan's §2).
        """
        if self._active is not None:
            return _state_of(self._active, datetime.now(tz=UTC))
        client, response = await open_audio_stream(base_url)
        if response.status_code != 200:
            detail = await _detail_of(response)
            await response.aclose()
            await client.aclose()
            raise RecorderRefused(detail, status=response.status_code)
        active = _Active(
            ctx=ctx,
            started_at=datetime.now(tz=UTC),
            frequency_hz=frequency_hz,
            mode=mode,
            bandwidth_hz=bandwidth_hz,
            gain=gain,
            serial=serial,
        )
        self._active = active
        # Drops a row an earlier recording finalized on its own without ever being
        # collected by a stop. It is already in the library — only the courtesy copy in
        # the stop response goes, and reporting a stale one as "just saved" would be
        # worse than not reporting it.
        self._saved = None
        active.task = asyncio.create_task(self._run(active, client, response))
        self._task = active.task
        return _state_of(active, active.started_at)

    async def stop(self) -> dict[str, Any] | None:
        """Stop, wait for the blob and the row, and return the row (or None).

        None means nothing was captured: either nothing was recording, or the stream gave
        us no bytes at all. It is deliberately not an error — Record off with nothing
        running is the idempotent half of a switch.
        """
        active = self._active
        if active is not None:
            active.stop.set()
        if self._task is not None:
            # `_run` swallows its own failures, so this awaits a task that does not
            # raise; shielding is unnecessary and would only orphan the finalize.
            await self._task
        saved, self._saved = self._saved, None
        return saved

    async def _run(
        self, active: _Active, client: httpx.AsyncClient, response: httpx.Response
    ) -> None:
        """Spool the stream into a blob, then measure it and write the row.

        Every failure is swallowed to the log: this runs as a background task, and an
        exception escaping it would be a recording that vanished with the only trace on a
        box whose owner has no terminal (CLAUDE.md #10).
        """
        sha: str | None = None
        try:
            sha = await self._blobs.put_stream(self._chunks(active, response))
        except Exception as exc:  # noqa: BLE001 — a lost blob must not kill the task
            log.warning("sdr_recorder.spool_failed", error=repr(exc))
        finally:
            with contextlib.suppress(Exception):
                await response.aclose()
            with contextlib.suppress(Exception):
                await client.aclose()
            self._active = None
        if sha is None or active.bytes <= 0:
            # A stream that produced nothing is not a clip. Writing the row anyway would
            # put a zero-second entry in the library that plays silence and cannot be
            # trimmed, for a Record press that never caught any audio.
            log.info("sdr_recorder.nothing_captured", bytes=active.bytes)
            return
        try:
            self._saved = await self._save(active, sha)
        except Exception as exc:  # noqa: BLE001 — the blob is on disk either way
            log.warning("sdr_recorder.save_failed", error=repr(exc), sha=sha)

    async def _save(self, active: _Active, sha: str) -> dict[str, Any]:
        """Measure the finished clip and write its row."""
        peaks, measured = await levels(self._blobs.path_for(sha))
        # The decoded length, not the wall clock: a stream the sidecar ended early stopped
        # producing audio long before Record was released, and the clip's real duration is
        # what the trim sheet places its handles against. Wall clock is the fallback for a
        # box where ffmpeg could not read the file.
        duration_s = (
            measured
            if measured is not None
            else max(0.0, (datetime.now(tz=UTC) - active.started_at).total_seconds())
        )
        return await self._repo.add(
            active.ctx,
            started_at=active.started_at,
            ended_at=active.started_at + timedelta(seconds=duration_s),
            duration_s=duration_s,
            frequency_hz=active.frequency_hz,
            mode=active.mode,
            bandwidth_hz=active.bandwidth_hz,
            gain=active.gain,
            serial=active.serial,
            blob_sha256=sha,
            bytes_=active.bytes,
            peaks=peaks,
        )

    async def _chunks(self, active: _Active, response: httpx.Response) -> AsyncIterator[bytes]:
        """The sidecar's bytes until Stop, and then a plain `return`.

        Returning rather than raising is the whole trick: `put_stream` sees a finished
        iterator, renames its spool file into place and hands back a digest. Anything
        thrown here would unlink the spool instead and the recording would be gone.

        The read is raced against the stop event rather than checked between chunks,
        because a wedged sidecar can hold a socket open sending nothing at all — and
        Stop must then still answer, rather than the owner watching a button that never
        comes back.
        """
        stream = response.aiter_bytes().__aiter__()
        stopped = asyncio.ensure_future(active.stop.wait())
        try:
            while not active.stop.is_set():
                nxt = asyncio.ensure_future(stream.__anext__())
                done, _ = await asyncio.wait((nxt, stopped), return_when=asyncio.FIRST_COMPLETED)
                if nxt not in done:
                    nxt.cancel()
                    return
                try:
                    chunk = nxt.result()
                except StopAsyncIteration:
                    return  # the session ended; keep what we have
                except Exception as exc:  # noqa: BLE001 — a dropped stream is not a loss
                    log.info("sdr_recorder.stream_ended", error=repr(exc))
                    return
                active.bytes += len(chunk)
                yield chunk
        finally:
            stopped.cancel()


def _state_of(active: _Active, moment: datetime) -> dict[str, Any]:
    return {
        "started_at": active.started_at.isoformat(),
        "seconds": max(0.0, (moment - active.started_at).total_seconds()),
        "bytes": active.bytes,
        "frequency_hz": active.frequency_hz,
        "mode": active.mode,
        "bandwidth_hz": active.bandwidth_hz,
        "serial": active.serial,
    }


_REFUSED = "the radio refused to stream audio"


async def _detail_of(response: httpx.Response) -> str:
    """The sidecar's own sentence out of a refusal, or a fallback.

    Read with `aread` because the response was opened as a stream: its body has not
    arrived yet, and reading `.text` on an unread stream raises rather than answering.
    """
    try:
        parsed = json.loads(await response.aread() or b"{}")
    except Exception:  # noqa: BLE001 — a body we cannot parse is not the interesting news
        return _REFUSED
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return str(detail) if detail else _REFUSED
