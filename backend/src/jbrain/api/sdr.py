"""The owner-only radio control API — what the omnibox tuner drives.

`GET /sdr/status` is the one the composer polls: its `listening` field is what lights
the radio icon, so "a session is running" and "the owner sees a radio icon" are the
same fact rather than two that can drift apart. Start/tune/stop move the radio;
`GET /sdr/audio` proxies the sidecar's live Opus so the browser fetches it
same-origin (the `radio` network is internal — the PWA has no route to the sidecar).

Every route is `OwnerDep`. The radio is a physical device on the owner's box: there
is no scoped-token or family case for it, so the surface is simply unreachable by
anything but the owner.

**No URL ever crosses this boundary.** Routes take a frequency and a mode, bounded
here as well as in the sidecar, and the sidecar's base URL is pinned in settings.
That is what keeps `stream.py`'s SSRF guard untouched rather than widened
(docs/plans/SDR_RADIO_PLAN.md §4.4).

This module touches no LLM (rule 1 n/a), no storage, and no database — the radio is
process state in the sidecar, not a row — so there is no RLS surface to scope. The
recordings library, which does have one, is a later wave.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import wave
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from jbrain.api.deps import OwnerDep, SettingsDep
from jbrain.api.notes import SessionMakerDep, ctx_for
from jbrain.db.session import scoped_session
from jbrain.sdr.aprslog import AprsReader
from jbrain.sdr.command import MAX_FAILURES
from jbrain.transcribe import WhisperCppClient

router = APIRouter(prefix="/sdr", tags=["sdr"])

# The R820T2's real range, and the demodulators rtl_fm offers. Bounded here as well
# as in the sidecar because a bound that lives only in the caller is not a bound.
MIN_MHZ = 0.024
MAX_MHZ = 1766.0
MODES = ("fm", "nfm", "wbfm", "am", "usb", "lsb")

# The lease purpose a logging session holds, and where APRS lives in North America
# when the owner does not say otherwise (APRS_CONTROL_PLAN.md §7 holds the private
# command frequency open).
APRS_PURPOSE = "aprs"
APRS_DEFAULT_MHZ = 144.39

# How far back a single caption may reach. Segments that pile up behind a busy whisper
# are transcribed TOGETHER rather than one at a time (see _Backlog), and this bounds how
# much audio one merged clip may carry: past it the oldest is given up, because a caption
# for something said half a minute ago is not a live caption any more.
CAPTION_BACKLOG_S = 24.0
# How long the caption stream waits with nothing to say before sending a comment to hold
# the socket open. Proxies close an idle event stream, and a quiet band is normal.
CAPTION_IDLE_S = 15.0

# Long enough for a slow tuner open, short enough that a wedged sidecar surfaces as
# an error rather than a hung composer.
_TIMEOUT = 30.0

# How many command attempts the radio tab shows. Enough to see a burst of failures at a
# glance; the whole history stays in the table.
ATTEMPTS_SHOWN = 20


class SdrStatusOut(BaseModel):
    """`listening` is None when the radio is idle — which is exactly the condition
    the omnibox uses to decide whether the icon exists at all."""

    available: bool
    listening: dict[str, Any] | None


def _base(settings: Any) -> str:
    url = cast(str, settings.sdr_url or "")
    if not url:
        raise HTTPException(status_code=503, detail="No SDR on this box.")
    return url


async def _post(settings: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=_base(settings), timeout=_TIMEOUT) as client:
        resp = await client.post(path, json=body)
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=_detail(resp, "The radio is busy."))
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"sdr sidecar: {_detail(resp, resp.text[:300])}"
        )
    return cast(dict[str, Any], resp.json())


def _detail(resp: httpx.Response, fallback: str) -> str:
    try:
        return cast(str, resp.json().get("detail") or fallback)
    except ValueError:
        return fallback


@router.get("/status")
async def status(settings: SettingsDep, _owner: OwnerDep) -> SdrStatusOut:
    """What the radio is doing. Answers `available: false` on a box with no radio
    rather than erroring, so the composer can simply never show the icon."""
    if not settings.sdr_url:
        return SdrStatusOut(available=False, listening=None)
    try:
        async with httpx.AsyncClient(base_url=settings.sdr_url, timeout=5.0) as client:
            resp = await client.get("/healthz")
        health = cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError):
        # The sidecar is configured but unreachable (starting, crashed). Idle is the
        # honest answer — the icon stays dark rather than lit over a dead radio.
        return SdrStatusOut(available=False, listening=None)
    return SdrStatusOut(available=True, listening=health.get("listening"))


@router.post("/listen")
async def listen(
    settings: SettingsDep,
    _owner: OwnerDep,
    frequency_mhz: Annotated[float, Query(gt=MIN_MHZ, lt=MAX_MHZ)],
    mode: Annotated[str, Query(pattern=f"^({'|'.join(MODES)})$")] = "wbfm",
    gain: Annotated[str | None, Query(max_length=16)] = None,
) -> dict[str, Any]:
    """Take the radio and start listening. 409 when it is already held."""
    return await _post(
        settings,
        "/listen/start",
        {"frequency_hz": int(round(frequency_mhz * 1_000_000)), "mode": mode, "gain": gain},
    )


@router.get("/packets")
async def packets(
    settings: SettingsDep,
    owner: OwnerDep,
    maker: SessionMakerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """The heard log, newest first, plus whether anything is receiving right now.

    `logging` is what separates a quiet channel from a dead one, and the tab shows the
    two differently — a watch that silently died is worse than no watch
    (docs/mocks/aprs/README.md). Rows carry text transmitted by strangers; the client
    renders them as untrusted."""
    base = _base(settings)
    health = await _health(base)
    session = (health.get("listening") or {}) if health else {}
    rows = await AprsReader(maker).recent(ctx_for(owner), limit=limit)
    return {
        # `logging` answers "is something receiving"; `reachable` answers "do we know".
        # Collapsing the two would make an unreachable sidecar read as a switched-off
        # one — a dead receiver looking exactly like a quiet channel, which is the
        # confusion this whole surface exists to prevent.
        "reachable": health is not None,
        "logging": session.get("purpose") == APRS_PURPOSE,
        "frequency_hz": session.get("frequency_hz") if session else None,
        "packets": [
            {
                "heard_at": row["heard_at"].isoformat(),
                "frequency_hz": row["frequency_hz"],
                "source": row["source"],
                "destination": row["destination"],
                "path": row["path"],
                "info": row["info"],
            }
            for row in rows
        ],
    }


@router.get("/commands")
async def commands(
    owner: OwnerDep,
    maker: SessionMakerDep,
) -> dict[str, Any]:
    """The owner's radio commands and what has been tried against them.

    The APRS tab's read-only summary (docs/mocks/aprs/a-launcher-shape.html, the
    "Automations · radio" section, and c-single-dongle's "armed but deaf" block). Two
    facts the owner cannot get anywhere else on this screen:

    **What is armed**, so "armed while nothing is receiving" is visible as the pair it
    is. Arming a command and enabling the receiver are separate switches on purpose,
    and a task that says armed while the radio is deaf is the same lie a signal meter
    on a dead channel tells.

    **What has been TRIED.** A refusal is the row worth having — three attempts against
    `GATE` from an unknown station last Tuesday is a fact the owner must be able to find
    afterwards, and a push notification does not keep. It is read here rather than
    pushed only (APRS_CONTROL_PLAN.md P4: "every attempt is visible").

    Editing lives in Tasks; this is a view."""
    ctx = ctx_for(owner)
    async with scoped_session(maker, ctx) as session:
        armed = (
            (
                await session.execute(
                    text(
                        "SELECT id, name, enabled, command_word, command_callsign,"
                        " command_days, command_from, command_until, command_failures,"
                        " command_last_at"
                        " FROM app.tasks WHERE schedule_kind = 'on_command'"
                        " ORDER BY command_word"
                    )
                )
            )
            .mappings()
            .all()
        )
        tried = (
            (
                await session.execute(
                    text(
                        "SELECT heard_at, source, word, accepted, reason"
                        " FROM app.command_attempts ORDER BY heard_at DESC LIMIT :n"
                    ),
                    {"n": ATTEMPTS_SHOWN},
                )
            )
            .mappings()
            .all()
        )
    return {
        "commands": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "enabled": row["enabled"],
                "word": row["command_word"],
                "callsign": row["command_callsign"],
                "days": list(row["command_days"] or []),
                "from": row["command_from"],
                "until": row["command_until"],
                "locked": int(row["command_failures"] or 0) >= MAX_FAILURES,
                "last_at": row["command_last_at"].isoformat() if row["command_last_at"] else None,
            }
            for row in armed
        ],
        "attempts": [
            {
                "heard_at": row["heard_at"].isoformat(),
                "source": row["source"],
                "word": row["word"],
                "accepted": row["accepted"],
                "reason": row["reason"],
            }
            for row in tried
        ],
    }


@router.post("/aprs")
async def aprs_logging(
    settings: SettingsDep,
    _owner: OwnerDep,
    enabled: Annotated[bool, Query()],
    frequency_mhz: Annotated[float, Query(gt=MIN_MHZ, lt=MAX_MHZ)] = APRS_DEFAULT_MHZ,
) -> dict[str, Any]:
    """Turn APRS logging on or off. 409 when something else holds the radio.

    The PWA's switch (`docs/mocks/aprs/c-single-dongle.html`, shape A). Idempotent in
    both directions, and turning it OFF stops the APRS session by id — never a
    listening session the owner started, which on a one-tuner box would silence the
    radio they were actually using."""
    base = _base(settings)
    health = await _health(base)
    if health is None:
        # An unreachable sidecar is not "off". Answering `{"logging": false}` here would
        # flip the switch to off in front of the owner while logging, if it is running,
        # carries on — the switch lying in the direction that looks harmless.
        raise HTTPException(status_code=502, detail="the radio isn't reachable")
    session = health.get("listening") or {}
    logging_now = session.get("purpose") == APRS_PURPOSE

    if not enabled:
        if not logging_now:
            return {"logging": False, "changed": False}
        stopped = await _post(settings, "/listen/stop", {"session_id": session.get("session_id")})
        if not stopped.get("stopped"):
            # The sidecar answers 200 `{"stopped": false}` when the id no longer matches
            # — the session changed between the health read and the stop. Whether that
            # matters depends on what is there NOW: if it ended on its own the owner has
            # what they asked for, and if a new session took the tuner, reporting "off"
            # is how a timed window leaves it held all day.
            after = await _health(base)
            still = ((after.get("listening") or {}) if after else {}).get("purpose")
            if still == APRS_PURPOSE:
                raise HTTPException(status_code=409, detail="the radio changed under us")
            return {"logging": False, "changed": False}
        return {"logging": False, "changed": True}

    if logging_now:
        return {"logging": True, "changed": False, "frequency_hz": session.get("frequency_hz")}
    body = await _post(
        settings,
        "/listen/start",
        {
            "frequency_hz": int(round(frequency_mhz * 1_000_000)),
            # 1200-baud AFSK is narrowband FM; nothing else can carry it.
            "mode": "fm",
            "gain": None,
            "purpose": APRS_PURPOSE,
        },
    )
    if body.get("purpose") != APRS_PURPOSE:
        # A sidecar too old to understand `purpose` IGNORES it and returns 200 with a
        # plain LISTENING session. Without this the switch says logging is on while
        # nothing decodes and the one tuner sits held on 144.39.
        raise HTTPException(
            status_code=502,
            detail="the radio software is too old to log APRS — nothing was changed",
        )
    return {"logging": True, "changed": True, "frequency_hz": body.get("frequency_hz")}


async def _health(base: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
            resp = await client.get("/healthz")
        return cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError):
        return None


@router.post("/tune")
async def tune(
    settings: SettingsDep,
    _owner: OwnerDep,
    frequency_mhz: Annotated[float, Query(gt=MIN_MHZ, lt=MAX_MHZ)],
    mode: Annotated[str | None, Query(pattern=f"^({'|'.join(MODES)})$")] = None,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Retune the live session. The session id survives, so the icon does not blink."""
    body: dict[str, Any] = {"frequency_hz": int(round(frequency_mhz * 1_000_000))}
    if mode is not None:
        body["mode"] = mode
    if session_id is not None:
        body["session_id"] = session_id
    return await _post(settings, "/listen/tune", body)


@router.post("/stop")
async def stop(
    settings: SettingsDep,
    _owner: OwnerDep,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Release the radio. This is what makes the omnibox icon disappear."""
    return await _post(settings, "/listen/stop", {"session_id": session_id})


@router.get("/audio")
async def audio(request: Request, settings: SettingsDep, _owner: OwnerDep) -> StreamingResponse:
    """Proxy the sidecar's live Opus to the browser.

    A proxy rather than a redirect because the `radio` network is internal: the PWA
    cannot reach the sidecar directly, and it should not be able to. Streaming it
    through here also means the audio inherits the owner session — an unauthenticated
    URL for the box's live radio is not something to hand out."""
    base = _base(settings)

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        try:
            async with client.stream("GET", "/listen/audio") as upstream:
                if upstream.status_code != 200:
                    return
                async for chunk in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(pump(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.get("/captions")
async def captions(request: Request, settings: SettingsDep, _owner: OwnerDep) -> StreamingResponse:
    """Live captions for whatever the radio is playing, as server-sent events.

    Whisper is not a streaming model — it transcribes a finished clip — so the sidecar
    cuts the live audio into segments on the quiet gaps between transmissions, and each
    one is transcribed and emitted as it lands. Words carry the per-token confidence the
    client already asks for, so the caption tints on the same rose-amber-green scale as
    the transcript viewer.

    **Reading and transcribing run apart.** The reader drains the sidecar as fast as it
    sends; whatever is waiting when whisper comes free is transcribed as ONE merged clip
    (`_Backlog`). Done in step instead, each whisper call stalls the reader, the
    sidecar's queue fills behind it, and the captioner settles permanently a backlog
    behind the live edge — which the client cannot correct for, because a caption that
    arrives after its audio was heard can only be shown late.

    **The model is deliberately NOT freed between segments.** Measured on this box
    (26 consecutive calls, model resident throughout): a transcription costs ~9.8 s
    whatever the clip holds. That is INFERENCE, not model loading — an earlier reading
    of the same flat number blamed load/unload, and the residency rule was written on
    that mistake. The rule survives being right for a different reason: unloading would
    add the load back on top of the 9.8 s, and the capture route pays exactly that
    because it unloads when it finishes. The cost is flat because whisper.cpp pads every
    clip to a 30 s window, which is also what makes merging a backlog free (`_Backlog`).
    It is why captions are an explicit toggle rather than always-on, too, since a
    resident whisper shares the GPU with the chat model.

    Segments arrive already squelched: the sidecar drops any whose loudest moment never
    crossed the floor, because whisper answers an empty band with fluent invented
    sentences rather than silence."""
    base = _base(settings)
    if not settings.whisper_url:
        raise HTTPException(
            status_code=503,
            detail="no whisper gateway on this box, so live captions cannot run",
        )
    whisper = WhisperCppClient(
        settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
    )

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        backlog = _Backlog()
        arrived = asyncio.Event()
        ended = asyncio.Event()

        async def read(upstream: httpx.Response) -> None:
            """Drain the sidecar as fast as it sends, whatever whisper is doing."""
            try:
                async for started, wav in _segments(upstream):
                    if wav is None:
                        continue  # a keep-alive; the loop below sends its own
                    backlog.add(started, wav)
                    arrived.set()
            finally:
                ended.set()
                arrived.set()

        try:
            async with client.stream("GET", "/listen/segments") as upstream:
                if upstream.status_code != 200:
                    yield _event({"error": "the radio is not listening"})
                    return
                reader = asyncio.create_task(read(upstream))
                try:
                    while not await request.is_disconnected():
                        # Cleared BEFORE looking, so a segment that lands between the
                        # look and the wait still wakes us rather than being slept on.
                        arrived.clear()
                        batch = backlog.take()
                        if batch is None:
                            if ended.is_set():
                                break
                            try:
                                await asyncio.wait_for(arrived.wait(), CAPTION_IDLE_S)
                            except TimeoutError:
                                yield ": keepalive\n\n"
                            continue
                        started, wav = batch
                        try:
                            result = await whisper.transcribe(
                                wav, filename="segment.wav", media_type="audio/wav"
                            )
                        except (httpx.HTTPError, ValueError) as exc:
                            # One bad segment must not end the caption stream; the next
                            # transmission is a fresh chance and the owner keeps listening.
                            yield _event({"error": str(exc)[:200], "started_at": started})
                            continue
                        text = result.text.strip()
                        if not text:
                            continue
                        yield _event(
                            {
                                "started_at": started,
                                "text": text,
                                "words": [
                                    {"text": w.text, "confidence": round(w.confidence, 4)}
                                    for w in result.words
                                ],
                            }
                        )
                finally:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader
        finally:
            await client.aclose()

    return StreamingResponse(
        pump(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


def _event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _segments(upstream: httpx.Response):
    """Split the sidecar's newline-framed stream into (started_at, wav) pairs.

    The frame is a JSON header line then exactly `bytes` of WAV. Framing rather than a
    request per segment because the gap between requests always lands mid-sentence."""
    buffer = b""
    async for block in upstream.aiter_bytes():
        buffer += block
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            try:
                head = json.loads(buffer[:newline] or b"{}")
            except json.JSONDecodeError:
                buffer = buffer[newline + 1 :]
                continue
            if head.get("keepalive"):
                buffer = buffer[newline + 1 :]
                yield 0.0, None
                continue
            size = int(head.get("bytes", 0))
            if size <= 0:
                # Not a segment frame — a blank line, or a header that lost its size.
                # Yielding it would hand whisper zero bytes of audio to describe.
                buffer = buffer[newline + 1 :]
                continue
            if len(buffer) < newline + 1 + size:
                break  # the WAV has not all arrived yet
            wav = buffer[newline + 1 : newline + 1 + size]
            buffer = buffer[newline + 1 + size :]
            yield float(head.get("started_at", 0.0)), wav


def _clip_seconds(wav: bytes) -> float:
    """How much audio a WAV clip holds, without reading its samples."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as src:
            return src.getnframes() / (src.getframerate() or 1)
    except (wave.Error, EOFError, OSError):
        return 0.0


def _merge(clips: list[bytes]) -> bytes:
    """Join consecutive WAV clips into one.

    The clips are contiguous slices of the same live capture, so concatenating their
    frames reproduces the audio exactly as it was on the air — there is no crossfade or
    resample to get wrong."""
    frames: list[bytes] = []
    rate = 0
    for clip in clips:
        try:
            with wave.open(io.BytesIO(clip), "rb") as src:
                frames.append(src.readframes(src.getnframes()))
                rate = src.getframerate() or rate
        except (wave.Error, EOFError, OSError):
            continue  # a truncated clip is dropped, not allowed to poison the batch
    if not frames or not rate:
        return clips[0] if clips else b""
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(b"".join(frames))
    return out.getvalue()


class _Backlog:
    """Segments waiting for whisper, so that READING never waits on TRANSCRIBING.

    Read in step with transcription — the shape this route had first — the reader stalls
    for the whole of every whisper call, the sidecar's queue fills behind it, and the
    captioner ends up working through audio that was on the air a minute ago. Because it
    never catches up, that lag is permanent: captions arrive long after the listener has
    heard the words, which is the one failure the client cannot correct for.

    What waits here is transcribed TOGETHER rather than one clip at a time. Whisper's
    cost on this box is flat in clip length (~10.7 s for 4 s of audio and for 11 s
    alike), so a merged clip costs what a single one does and loses no words — where
    taking only the newest would silently drop whole sentences. The cap is what keeps a
    merge from reaching back further than a live caption sensibly can.
    """

    def __init__(self, max_seconds: float = CAPTION_BACKLOG_S) -> None:
        self._max = max_seconds
        self._held: list[tuple[float, bytes, float]] = []

    def add(self, started: float, wav: bytes) -> None:
        self._held.append((started, wav, _clip_seconds(wav)))
        # Give up the OLDEST past the cap. Dropping the newest instead would leave the
        # captioner reading history while the live edge went by unseen.
        while len(self._held) > 1 and sum(c[2] for c in self._held) > self._max:
            self._held.pop(0)

    def take(self) -> tuple[float, bytes] | None:
        """Everything waiting, as one clip stamped with the first segment's start."""
        if not self._held:
            return None
        held, self._held = self._held, []
        if len(held) == 1:
            return held[0][0], held[0][1]
        return held[0][0], _merge([wav for _, wav, _ in held])
