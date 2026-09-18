"""Recording the live radio: one more subscriber on the sidecar's audio stream.

docs/plans/SDR_RECORDING_PLAN.md R1. The api records rather than the sidecar because
`deploy/docker-compose.yml` gives the `sdr` service no volume and 512 MB — anything it
wrote would die on the next Ops → Update, and it cannot hold the owner's data under RLS
either. So this opens its own `GET /listen/audio` (the same fan-out the browser's
`<audio>` uses, so recording never interrupts listening) and spools the bytes straight
into `blobs.put_stream(...)`: a clip is never buffered whole in memory, whatever its
length, and it lands through the storage abstraction as CLAUDE.md #2 requires.

**One recording at a time, enforced with a LOCK.** Not a limitation to work around:
there is one listen session, and a second recorder would spool a byte-identical second
copy of it — which content-addressing would then dedupe into one blob with two rows
pointing at it, so deleting either would take the other's audio. A bare `is None` check
does not enforce that, because opening the stream awaits: two Record presses both see
"not recording", both connect, and the loser is orphaned with an unreachable stop event,
spooling 28.8 MB/hour that nothing can stop. So the check, the connect and the assignment
happen under one `asyncio.Lock`, and teardown clears the slot only if it still OWNS it —
an ended recording must never wipe a live one's slot, which is how `/sdr/status` came to
report nothing recording while Stop hung for ever.

**A capture is bounded, and refused when the disk is nearly full.** Nothing expires by
design (the plan's §1), so the only thing standing between a forgotten Record press and a
full volume is this — and the owner has no terminal to clear one with (CLAUDE.md #10).
The bound stops and SAVES rather than discarding: an interrupted recording is still a
recording, whether the interruption is the sidecar or us.

**An interrupted recording is still a recording.** The sidecar ending a session, a
dropped connection, or a retune that outlives the stream all close it from the far end,
and each of those must finalize the blob and write its row rather than discard what was
captured. That is why `_chunks` RETURNS on every ending — a raised exception would
propagate out of `put_stream`, unlink the spool file, and lose the audio.

**Two kinds, one recorder.** Long-pressing Record swaps what it captures: `'audio'` is
everything above, `'captions'` is the SAME reception transcribed and nothing else — the
sidecar's WAV segments off `GET /listen/segments`, through whisper, into the row's
`transcript`. The owner chose captions INSTEAD of audio, so a captions recording writes
**no blob at all** and its `blob_sha256`, `bytes` and `peaks` are NULL rather than a
digest of nothing, a zero and an empty envelope. The kind picks the SOURCE — which
stream is opened and what the bytes become — and everything the two share (the gate, the
slot, the settings on the row, an ending that still saves) is written once.

**The gate stays box-wide across kinds, not per-kind.** There is one radio and one
listen session, and both kinds are a subscriber on it, so a captions capture running
beside an audio one would be two recordings of one reception — which `/sdr/status`
cannot even report, since it carries ONE `recording` that the tape deck draws. The
button is a SWAP, not a second button: the long press chooses what the one Record does.

**Nothing here touches the caption stream the PWA may have open.** Each captioner
subscribes separately on the sidecar (`sdr/captions.py`), so a captions recording starts
its own segments rather than requiring CC to be on first — the owner has no terminal and
"turn CC on, then press Record" is a worse answer than just doing it (CLAUDE.md #10) —
and stopping closes only what this recording opened. CC is therefore left exactly as it
was found, on or off: a recording that silently switched the owner's live captions on,
or off, would be this feature reaching outside what it was asked to do.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import httpx
import structlog

from jbrain.db.session import SessionContext
from jbrain.sdr.audio import levels
from jbrain.sdr.captions import Backlog, segments
from jbrain.sdr.recordings import RecordingsRepo
from jbrain.storage import BlobStore
from jbrain.transcribe import TranscribeClient

log = structlog.get_logger(__name__)

#: Connect promptly, then read for as long as the owner records. `timeout=None` outright
#: would also make a sidecar that never accepts the connection hang the Record button
#: with nothing to say.
CONNECT_TIMEOUT_S = 10.0

#: What Record captures. The kind picks the SOURCE — which sidecar stream is opened and
#: what the bytes become — rather than branching inside the spool.
Kind = Literal["audio", "captions"]

#: The longest one AUDIO capture may run, as the bytes it costs. 64 kbps mono MP3 is
#: 8 kB/s (`deploy/sdr/listen.py`), so this is four hours — longer than any net or event
#: the radio is pointed at, and 115 MB rather than the 691 MB a Record press forgotten
#: for a day would spool. Reaching it ends the recording the same way the sidecar
#: dropping the session does: the blob is finalized and the row written, so the owner
#: finds four hours in the library rather than a gap where a day went.
#:
#: **It bounds AUDIO only.** Four hours is what this number means; bytes are merely how
#: audio spends them, and a captions capture spends none at all — measuring one against
#: a bitrate it does not have is the "8 kB/s" arithmetic applied to something that is
#: not audio, which is how a bound stops bounding anything.
MAX_CAPTURE_BYTES = 4 * 60 * 60 * (64_000 // 8)

#: The longest one CAPTIONS capture may run, in seconds — the same four hours, stated
#: directly because there is no bitrate to state it through.
#:
#: The bound is WALL CLOCK rather than transcript size, because size is not what a
#: forgotten captions recording costs. It writes no blob, so it cannot fill the volume;
#: what it does hold is a segment subscription on the sidecar and a place in the queue
#: in front of whisper, which stays resident and shares the GPU with the chat model for
#: as long as it runs (see `api/sdr.py` `GET /sdr/captions`). And a capture pointed at a
#: quiet band accumulates NOTHING — the sidecar squelches silence — so a bound on what
#: has been transcribed would never be reached by the very recording that most needs
#: ending. Four hours of continuous speech is around 200 000 characters, which is also
#: what keeps the `transcript` column inside what one response can hand a phone.
MAX_CAPTION_CAPTURE_S = 4 * 60 * 60

#: How long the captions loop may sit with nothing to transcribe before looking up. A
#: quiet band is normal and sends nothing at all, so the wall-clock bound above needs a
#: tick of its own or it would only be checked when somebody next spoke.
_CAPTION_TICK_S = 5.0

#: How long Stop may wait for the LAST clip to come back from whisper. Measured on this
#: box: a transcription costs about ten seconds whatever the clip holds (`api/sdr.py`
#: `GET /sdr/captions`), so this is six times the real figure — and it exists because
#: `settings.whisper_timeout` defaults to five MINUTES, which is a fine bound for a
#: background job and a terrible one for a button the owner is holding. Running out
#: costs the final transmission and nothing else: everything transcribed before it is
#: already on the recording and the row is written either way.
_FINAL_CAPTION_S = 60.0

#: Refuse to start, and stop a running capture, below this much free space on the blob
#: volume. A box that fills its disk stops being fixable from the PWA — Postgres stops
#: accepting writes and Ops → Update cannot pull an image — and a recording is the one
#: thing here that grows without being asked to. 1 GiB leaves room to trim and delete
#: (which need to WRITE a cut clip before they can free anything).
MIN_FREE_BYTES = 1 << 30

#: How often the free-space floor is re-checked while spooling. At 8 kB/s this is about
#: every two minutes — a `statvfs` per 1 MiB of audio is free, and the alternative is
#: checking only at the start, which is no protection at all against a four-hour capture
#: running beside everything else the box does.
_FREE_CHECK_EVERY_BYTES = 1 << 20


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


async def open_caption_stream(base_url: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open the sidecar's WAV segments, returning the client so the caller can close both.

    Its own subscription, deliberately: the sidecar fans `subscribe_segments` out to a
    queue per subscriber, so this neither steals the live caption stream's segments nor
    needs one to exist. That is what lets Record start captions with CC off.

    A module function for the same reason as `open_audio_stream` — a test replaces it,
    and nothing below should need a socket to prove.
    """
    client = httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S)
    )
    try:
        request = client.build_request("GET", "/listen/segments")
        return client, await client.send(request, stream=True)
    except BaseException:
        await client.aclose()
        raise


async def _open_source(kind: Kind, base_url: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    """The stream this kind records: the live MP3, or the WAV segments.

    The ONE place the kind decides where the bytes come from — everything after this
    point differs in what it does with them, not in where it got them. Resolved by name
    at the call rather than held in a table built at import, because both openers are
    module functions precisely so a test can replace them, and a table would have
    captured the originals before any test could.
    """
    opener = open_audio_stream if kind == "audio" else open_caption_stream
    return await opener(base_url)


@dataclass
class _Caption:
    """One transcribed transmission, as it will sit in the row's `transcript`.

    `at_s` is seconds from the START of the recording, not the epoch stamp the sidecar
    sends: an offset is the only reading that survives being looked at later, and it is
    what a caption list scrubs against. Both clocks are the host's — the sidecar is a
    container on the same kernel — so the subtraction is a real measurement rather than
    two clocks compared.
    """

    at_s: float
    text: str
    words: list[dict[str, Any]]


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
    #: Defaulted so that `audio` — everything this class meant before there was a second
    #: kind — stays the thing you get without saying anything.
    kind: Kind = "audio"
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    bytes: int = 0
    #: What a CAPTIONS capture has heard so far, in the order it was said. Empty on an
    #: audio recording, and empty on a captions one that caught nothing — which is the
    #: same "no row" case as a stream that gave no audio.
    captions: list[_Caption] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    #: The row THIS recording wrote, once it has. Held on the recording rather than on
    #: the recorder so that a stop can only ever be handed the row belonging to the
    #: recording it stopped — a shared slot lets a clip that finished late be reported as
    #: the saved result of the next one.
    saved: dict[str, Any] | None = None


class SdrRecorder:
    """Records the live listen session to a blob, then to a row. At most one at a time."""

    def __init__(self, blobs: BlobStore, repo: RecordingsRepo) -> None:
        self._blobs = blobs
        self._repo = repo
        self._active: _Active | None = None
        # The most recent recording, kept past the end of the stream it belongs to.
        # `_active` is cleared the moment the stream ends, so a stop arriving while the
        # row is still being written would otherwise find nothing to wait for and report
        # that nothing was saved — about a clip that is landing in the library as it
        # answers. It is also what a stop reads the saved row OFF, which is why the row
        # lives on the recording: the alternative, one `_saved` slot on the recorder, is
        # writable by a clip that ended before the current one started.
        self._finishing: _Active | None = None
        # Start and stop are mutually exclusive, and the exclusion spans the awaits.
        # `if self._active is None` then `await open_audio_stream(...)` is a check with a
        # yield point inside it: two Record presses both pass it, both open a stream, and
        # the loser becomes an orphan nothing can reach to stop.
        self._gate = asyncio.Lock()

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
        kind: Kind = "audio",
        transcriber: TranscribeClient | None = None,
    ) -> dict[str, Any]:
        """Begin recording, or return the recording already running.

        Idempotent at the source rather than only in the route: two Record presses a
        moment apart are two requests that both saw "not recording", and the second must
        not open a second stream.

        The settings stored are the ones in force NOW. A retune does not restart the
        pipeline, so a recording can span a frequency change; the row keeps where it
        started, which is what the library shows (the plan's §2). A captions recording
        keeps them too — what was heard is worth as little without where it was heard as
        a clip would be.

        Held under `_gate` for the whole of it — the check, the connect and the
        assignment. The connect is a real socket, and a second press arriving inside it
        is the ordinary case (a double tap, two tabs), not a rare one. **The gate is
        box-wide across both kinds** (see this module's docstring): the second press
        gets back the recording already running, whichever kind it is, because there is
        one radio and the status carries one recording.

        The free-space floor is checked for BOTH kinds, and that is not an oversight. A
        captions capture writes no blob, but it does write a row — and a Postgres that
        has run out of volume refuses that write along with everything else, so starting
        one on a full disk would spend four hours of whisper on a transcript with
        nowhere to land.
        """
        async with self._gate:
            if self._active is not None:
                return _state_of(self._active, datetime.now(tz=UTC))
            if kind == "captions" and transcriber is None:
                # Reached only by a caller that skipped the route's check; the sentence
                # is the same one, because it is the owner who has to read it.
                raise RecorderRefused(
                    "There is no whisper gateway on this box, so captions cannot be "
                    "recorded. Record audio instead.",
                    status=503,
                )
            free = self._blobs.free_bytes()
            if free < MIN_FREE_BYTES:
                raise RecorderRefused(
                    f"There is only {free / (1 << 20):.0f} MB left on the box, so there is "
                    "nowhere to put a recording. Trim or delete something first.",
                    status=400,
                )
            client, response = await _open_source(kind, base_url)
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
                kind=kind,
            )
            self._active = active
            # Whatever an earlier recording finalized on its own is no longer collectable
            # by a stop: it is already in the library, and reporting a stale row as "just
            # saved" would be worse than not reporting it. Moving the pointer (rather
            # than clearing a shared slot) is what makes that safe while that earlier
            # recording's finalize may still be running — its row lands on ITS object.
            self._finishing = active
            if kind == "audio":
                runner = self._run(active, client, response)
            else:
                # Not None — the gate refused a captions start without one, above.
                runner = self._run_captions(
                    active, client, response, cast(TranscribeClient, transcriber)
                )
            active.task = asyncio.create_task(runner)
            return _state_of(active, active.started_at)

    async def stop(self) -> dict[str, Any] | None:
        """Stop, wait for the blob and the row, and return the row (or None).

        None means nothing was captured: either nothing was recording, or the stream gave
        us no bytes at all. It is deliberately not an error — Record off with nothing
        running is the idempotent half of a switch.

        Takes the same gate as `start`, so a stop that arrives while a start is still
        connecting waits for it and then stops what it started, rather than answering
        "nothing was recording" about a recording that begins a millisecond later.
        """
        async with self._gate:
            active = self._active
            if active is not None:
                active.stop.set()
            finishing, self._finishing = self._finishing, None
            if finishing is None:
                return None
            if finishing.task is not None:
                # `_run` swallows its own failures, so this awaits a task that does not
                # raise; shielding is unnecessary and would only orphan the finalize.
                await finishing.task
            return finishing.saved

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
            # ONLY if this recording still owns the slot. Clearing it unconditionally
            # lets a recording that ended wipe a live one's — after which `/sdr/status`
            # reports nothing recording, Stop finds nothing to signal and waits for ever,
            # and the shutdown finalize burns its whole timeout and then cancels the very
            # save it exists to perform.
            if self._active is active:
                self._active = None
        if sha is None or active.bytes <= 0:
            # A stream that produced nothing is not a clip. Writing the row anyway would
            # put a zero-second entry in the library that plays silence and cannot be
            # trimmed, for a Record press that never caught any audio.
            log.info("sdr_recorder.nothing_captured", bytes=active.bytes)
            return
        try:
            active.saved = await self._save(active, sha)
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

    async def _run_captions(
        self,
        active: _Active,
        client: httpx.AsyncClient,
        response: httpx.Response,
        transcriber: TranscribeClient,
    ) -> None:
        """Transcribe the live session until Stop, then write the row. No blob at all.

        The audio twin of this spools bytes; this one spools SENTENCES, and the shape of
        the ending is the same in both: every failure is swallowed to the log, because a
        background task that raises is a recording that vanished on a box whose owner has
        no terminal (CLAUDE.md #10), and every way the stream can end still saves what was
        heard.
        """
        try:
            await self._caption_loop(active, response, transcriber)
        except Exception as exc:  # noqa: BLE001 — a lost transcript must not kill the task
            log.warning("sdr_recorder.captions_failed", error=repr(exc))
        finally:
            with contextlib.suppress(Exception):
                await response.aclose()
            with contextlib.suppress(Exception):
                await client.aclose()
            # Only if this recording still owns the slot — the same rule, and the same
            # failure if it is broken, as the audio path's teardown.
            if self._active is active:
                self._active = None
        if not active.captions:
            # Nothing was said, or everything said was squelched as noise. A row with an
            # empty transcript is the captions twin of a clip that plays silence: it
            # reads in the library as a recording and holds nothing.
            log.info("sdr_recorder.nothing_captioned")
            return
        try:
            active.saved = await self._save_captions(active)
        except Exception as exc:  # noqa: BLE001 — the transcript is only in memory here
            log.warning("sdr_recorder.save_failed", error=repr(exc), kind="captions")

    async def _caption_loop(
        self, active: _Active, response: httpx.Response, transcriber: TranscribeClient
    ) -> None:
        """Read segments and transcribe them, with READING never waiting on WHISPER.

        The split is `api/sdr.py`'s and for its reason: a transcription costs about ten
        seconds whatever the clip holds, so a loop that read and transcribed in step would
        stall the reader for every call, fill the sidecar's queue behind it and settle
        permanently behind the live edge. What is waiting when whisper comes free is
        transcribed as ONE merged clip (`Backlog`), which costs the same and loses no
        words.

        **Stop takes one last batch rather than dropping it.** Whatever was said in the
        seconds before the button is exactly what the owner pressed Record for, and the
        stop path already waits on a finalize (the audio one decodes the whole clip for
        its waveform). Only ONE final batch, because the backlog merges — so this is a
        single bounded whisper call, not a queue drained to the end.
        """
        backlog = Backlog()
        arrived = asyncio.Event()
        ended = asyncio.Event()

        async def read() -> None:
            try:
                async for started, wav in segments(response):
                    if wav is None:
                        continue  # a keep-alive: nothing to transcribe
                    backlog.add(started, wav)
                    arrived.set()
            except Exception as exc:  # noqa: BLE001 — a dropped stream is not a loss
                log.info("sdr_recorder.captions_stream_ended", error=repr(exc))
            finally:
                ended.set()
                arrived.set()

        reader = asyncio.create_task(read())
        stopped = asyncio.ensure_future(active.stop.wait())
        try:
            while not active.stop.is_set():
                if _caption_bound_reached(active):
                    log.info("sdr_recorder.capture_bound_reached", captions=len(active.captions))
                    break
                # Cleared BEFORE looking, so a segment that lands between the look and
                # the wait still wakes us rather than being slept on.
                arrived.clear()
                batch = backlog.take()
                if batch is None:
                    if ended.is_set():
                        break
                    await _wait_for_segment(arrived, stopped)
                    continue
                await _transcribe_into(active, transcriber, batch)
            final = backlog.take()
            if final is not None:
                try:
                    await asyncio.wait_for(
                        _transcribe_into(active, transcriber, final), _FINAL_CAPTION_S
                    )
                except TimeoutError:
                    log.info("sdr_recorder.final_caption_timed_out")
        finally:
            stopped.cancel()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    async def _save_captions(self, active: _Active) -> dict[str, Any]:
        """Write the transcript row — and no blob, byte count or waveform.

        `duration_s` is the WALL CLOCK the capture ran for, which for captions is the
        only length there is: nothing was decoded, so there is nothing to measure. It is
        an honest number for this kind precisely where it would be a fallback for audio.
        """
        ended_at = datetime.now(tz=UTC)
        return await self._repo.add_captions(
            active.ctx,
            started_at=active.started_at,
            ended_at=ended_at,
            duration_s=max(0.0, (ended_at - active.started_at).total_seconds()),
            frequency_hz=active.frequency_hz,
            mode=active.mode,
            bandwidth_hz=active.bandwidth_hz,
            gain=active.gain,
            serial=active.serial,
            transcript=_transcript_of(active),
            transcribed_at=ended_at,
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

        It is also where a capture ends itself: at `MAX_CAPTURE_BYTES`, or when the blob
        volume drops below `MIN_FREE_BYTES`. Both return rather than raise, for the same
        reason as every other ending here — what has been captured is kept.
        """
        stream = response.aiter_bytes().__aiter__()
        stopped = asyncio.ensure_future(active.stop.wait())
        checked_at = 0
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
                if active.bytes >= MAX_CAPTURE_BYTES:
                    log.info("sdr_recorder.capture_bound_reached", bytes=active.bytes)
                    return
                if active.bytes - checked_at >= _FREE_CHECK_EVERY_BYTES:
                    checked_at = active.bytes
                    free = self._blobs.free_bytes()
                    if free < MIN_FREE_BYTES:
                        log.warning("sdr_recorder.disk_nearly_full", free_bytes=free)
                        return
        finally:
            stopped.cancel()


def _caption_bound_reached(active: _Active) -> bool:
    return (datetime.now(tz=UTC) - active.started_at).total_seconds() >= MAX_CAPTION_CAPTURE_S


async def _wait_for_segment(arrived: asyncio.Event, stopped: asyncio.Future[Any]) -> None:
    """Sleep until a segment lands, Stop is pressed, or the tick — whichever is first.

    The tick is what makes the wall-clock bound real on a quiet band: nothing arrives and
    nobody presses anything, and without it the loop would next look up when somebody
    spoke, which on an empty frequency is never.
    """
    waiting = asyncio.ensure_future(arrived.wait())
    try:
        await asyncio.wait(
            (waiting, stopped), timeout=_CAPTION_TICK_S, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        waiting.cancel()


async def _transcribe_into(
    active: _Active, transcriber: TranscribeClient, batch: tuple[float, bytes]
) -> None:
    """Transcribe one (merged) clip and keep it, or keep nothing and carry on.

    A failed transcription ends the CLIP, never the recording: the next transmission is a
    fresh chance, and a recording that stopped itself because whisper hiccuped would lose
    the hour after the hiccup as well as the sentence during it.
    """
    started, wav = batch
    try:
        result = await transcriber.transcribe(wav, filename="segment.wav", media_type="audio/wav")
    except Exception as exc:  # noqa: BLE001 — one bad clip is not the end of the capture
        log.info("sdr_recorder.caption_failed", error=repr(exc))
        return
    text = result.text.strip()
    if not text:
        return  # silence, or a clip whisper had nothing to say about
    at_s = max(0.0, started - active.started_at.timestamp())
    offset_ms = int(at_s * 1000)
    active.captions.append(
        _Caption(
            at_s=at_s,
            text=text,
            # Shifted onto the RECORDING's clock. Whisper times a word from the start of
            # the clip it was handed, and those clips are minutes apart in a capture —
            # left unshifted, every transmission would claim to have happened in the
            # first few seconds.
            words=[
                {
                    "text": w.text,
                    "start_ms": offset_ms + w.start_ms,
                    "end_ms": offset_ms + w.end_ms,
                    "confidence": round(w.confidence, 4),
                }
                for w in result.words
            ],
        )
    )


def _transcript_of(active: _Active) -> dict[str, Any]:
    """What lands in `transcript` — the shape every other transcript in the repo has.

    `{text, words, duration_ms}` is what `ingest/video.py` writes and what
    `AudioTranscript.tsx` renders, so a captions recording needs no viewer of its own.
    `segments` is the ADDITION, and it earns its place: whisper emits per-word timings
    only on builds that report them, and without it a text-only build would leave a
    four-hour capture as one undated paragraph with no way to tell one transmission from
    the next. It is the one timing that always survives.
    """
    return {
        "text": "\n".join(caption.text for caption in active.captions),
        "words": [word for caption in active.captions for word in caption.words],
        "duration_ms": int(
            max(0.0, (datetime.now(tz=UTC) - active.started_at).total_seconds()) * 1000
        ),
        "segments": [
            {"at_s": round(caption.at_s, 3), "text": caption.text} for caption in active.captions
        ],
    }


def _state_of(active: _Active, moment: datetime) -> dict[str, Any]:
    return {
        "started_at": active.started_at.isoformat(),
        "seconds": max(0.0, (moment - active.started_at).total_seconds()),
        "kind": active.kind,
        # A size for audio, a caption count for captions, and NULL for the one the
        # recording is not. Reporting 0 bytes under a captions capture would put a
        # measurement on the tape deck for something that is not being measured — and
        # the running figure is the argument for stopping, so it has to be the real one.
        "bytes": active.bytes if active.kind == "audio" else None,
        "captions": len(active.captions) if active.kind == "captions" else None,
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
