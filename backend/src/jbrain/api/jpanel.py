"""jpanel — voice post between the twins' panels and the owner (JPANEL_PLAN.md §3b).

**Asynchronous post, not a call**, and that word decides everything here. A message is
recorded, stored, and waits until the recipient chooses to play it. A four-year-old cannot be
expected to be present at the moment their sister speaks, so nothing rings and an undelivered
message simply waits on the box.

**Its own module rather than more of `endpoint.py`, and its own routes rather than more of
`/converse`.** That route is a request/response turn: audio up, reply down, nothing stored,
nothing addressed. This is the opposite on all three — addressed to a principal, persists until
played, and the reply may arrive hours later from a different device. A route that sometimes
answers and sometimes files has failure modes nobody can name.

**The asymmetry is the product.** Panels send audio and never read; the PWA sends text that TTS
speaks to them, and reads a transcript with the audio as a fallback. A four-year-old cannot
type and a parent at work cannot play audio out loud.

Isolation lives in Postgres (migration 0208), not in these handlers: a panel may read only what
it sent or was sent, may only insert rows that say they came from it, and may only mark its own
inbox played. The handlers are written as if the policies did not exist, because that is the
only way to find out whether they do.
"""

from __future__ import annotations

import time
from typing import Literal, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from jbrain.api.deps import OwnerDep, PanelDep
from jbrain.api.endpoint import (
    PANEL_RATE,
    _pcm_from_wav,
    _to_panel_rate,
    _trim_to_speech,
    _wav,
)
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.transcribe import WhisperCppClient

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/jpanel", tags=["jpanel"])

# A panel's clip: longer than a conversational turn, because a message is a thought rather
# than an answer, and short enough that a pocket-dial cannot fill the box.
MAX_MESSAGE_MS = 20_000
MAX_MESSAGE_BYTES = PANEL_RATE * 2 * MAX_MESSAGE_MS // 1000

# DAD DOES NOT SOUND LIKE THE PET. The pet answers in a female Kokoro voice; a message from a
# father arriving in the toy's own voice would teach a four-year-old that the robot and their
# parent are the same thing. A different voice is the cheapest possible signal that this is a
# person, and it costs one query parameter.
DAD_VOICE = "am_michael"
DAD_NAME = "Dad"


class SendResult(BaseModel):
    id: str
    to_name: str


class Waiting(BaseModel):
    """What the panel polls for. Deliberately tiny — it runs every ~30 s per panel forever."""

    count: int = 0
    from_name: str = ""


class Message(BaseModel):
    id: str
    from_name: str
    to_name: str
    direction: Literal["in", "out"]
    transcript: str
    composed: Literal["voice", "text"]
    duration_ms: int
    created_at: str
    played_at: str | None = None


class PanelThread(BaseModel):
    device_id: str
    name: str
    unplayed: int
    messages: list[Message]


class Threads(BaseModel):
    panels: list[PanelThread]


class SendText(BaseModel):
    """The owner composing. Text only — the PWA never uploads audio, by design."""

    to_device: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=600)


# What `/flash` writes into a panel principal's label, and the two shapes it can take:
# `f"panel {name}"` when the owner named the unit, `"room endpoint panel"` when they did not.
# Both are matched, because an unnamed panel is still a panel — a first cut of this matched
# only `panel%` and silently lost every unit flashed without a name.
_UNNAMED_LABEL = "room endpoint panel"


def _display_name(label: str) -> str:
    """The name to say out loud, from a principal's label. Pure, so the mapping can be pinned
    against the route that writes it rather than assumed."""
    if label == _UNNAMED_LABEL:
        # Sayable, if inelegant. A four-year-old told "a message from the other one" at least
        # knows a message arrived; an empty name would draw a pop-up from nobody.
        return "the other one"
    return label.removeprefix("panel").strip() or "the other one"


# ADDRESSING IS THE BOX'S JOB, NOT THE PANEL'S, AND RLS IS WHY.
#
# `principals_select` opens for the owner, for `auth_ctx()` in ('login','bootstrap'), and for a
# principal reading ITS OWN ROW — nothing else. So `_panel_names` run under a panel's own
# context (`ctx_for(principal)`, which is what `send` used) returns exactly one row: the panel
# asking. `others` was therefore ALWAYS empty and every panel-to-panel message answered 409,
# on any box, from the first commit. It is not a data problem and no amount of tidying the
# principals table would have fixed it.
#
# Resolved by reading the roster under the same narrow auth context the login path uses, in a
# session of its own, rather than by widening the policy. That ordering is the security
# posture and not merely a workaround: a panel must NOT be able to enumerate principals — it
# says "the other panel" and the box decides who that is. Widening `principals_select` to let
# device keys see each other would hand a device on a bedroom wall the whole principal table,
# `key_hash` column included, to fix an addressing question the panel should never have been
# asking.
#
# The session is read-only by construction: the two statements below are SELECTs, and `login`
# grants no INSERT or UPDATE on principals (`principals_update` needs owner or bootstrap).
_ADDRESSING = SessionContext(auth_context="login")


async def _panel_names(maker) -> dict[str, str]:
    """Panel principal id → the name to say out loud.

    TAKES THE SESSION MAKER, NOT A SESSION, so no caller can hand it one that cannot see the
    answer. Three of the five call sites ran under a panel's own context and therefore read a
    roster containing exactly one panel — themselves. That is the bug above, and it presented
    three different ways: `send` refused every sibling, the pop-up never learned who a message
    was from, and `GET /next`'s `X-Jpanel-From` always said "the other one". One cause, three
    symptoms, none of which looks like a permissions problem from the outside.

    THIS IS A LABEL CONVENTION, NOT A MECHANISM, and that is worth saying plainly. Panels are
    ordinary `device_key` principals — the same substrate as an OwnTracks phone — and the only
    thing distinguishing one is the label `/flash` writes.

    Used anyway because the alternative is a schema change to mark a principal kind the auth
    model does not have, and because the blast radius is small: the worst case is a message
    offered to a device that RLS then refuses to deliver to — a dead letter, not a leak. Worth
    replacing with a real marker the first time a third device key exists in this house
    (JPANEL_PLAN.md §5).

    ONE ROW PER NAME, NEWEST KEY WINS, AND WITHOUT THAT VOICE POST DOES NOT WORK AT ALL.

    Every `/flash` mints a fresh device key and nothing retires the old one, so a panel
    re-flashed thirteen times is thirteen unrevoked principals carrying the same label. On the
    live box that made fifteen candidates where `send(to="panel")` needs exactly one, so every
    panel-to-panel message answered 409 — a feature that could never have worked in this
    house, found by counting rows rather than by reading code.

    `DISTINCT ON (label)` with the newest `created_at` is not a tidy-up, it is the right
    answer: `/flash` rewrites the unit's NVS with the new key, so for a given name the newest
    principal IS the one that panel is now using and every older one is dead by construction.
    No liveness signal is needed to know that, which is why this does not wait on one.

    THE COST IS NAMING. Two physical panels flashed with the SAME name collapse to one row and
    one twin becomes unreachable. That is not a regression — a child saying "send a message"
    could not have picked between two panels called Elora either — but it is now the ONE thing
    that breaks addressing, so it is logged loudly rather than left to be discovered."""
    async with scoped_session(maker, _ADDRESSING) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (label) id::text, label
                    FROM app.principals
                    WHERE kind = 'device_key' AND revoked_at IS NULL
                      AND (label LIKE 'panel%' OR label = :unnamed)
                    ORDER BY label, created_at DESC
                    """
                ),
                {"unnamed": _UNNAMED_LABEL},
            )
        ).all()
        total = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM app.principals
                    WHERE kind = 'device_key' AND revoked_at IS NULL
                      AND (label LIKE 'panel%' OR label = :unnamed)
                    """
                ),
                {"unnamed": _UNNAMED_LABEL},
            )
        ).scalar_one()
    if int(total) > len(rows):
        # Not a warning: superseded keys are the NORMAL state of a re-flashed panel. It is
        # logged so that "why is there only one panel" has an answer without a database.
        log.info("jpanel.panels_collapsed", live=len(rows), keys=int(total))
    return {str(pid): _display_name(str(label)) for pid, label in rows}


async def _unplayed_by_panel(session) -> dict[str, int]:
    """Panel id → how many of its messages the OWNER has not dealt with.

    A function rather than inline SQL so it can be exercised directly, because two bugs live in
    the obvious version and both are invisible until someone is staring at a wrong number on a
    phone:

    - **Counting the rows the list query fetched** deflates the badge as soon as `limit`
      truncates. A parent who sees "2 waiting" when four are stops trusting the number, and a
      badge nobody trusts is worse than no badge.
    - **Counting everything a panel sent** includes twin-to-twin post, which is not the owner's
      to clear — so the badge would show a count he can never make go away.

    Hence both predicates: from a panel, TO the owner, unplayed."""
    rows = (
        await session.execute(
            text(
                """
                SELECT sender_device, count(*)
                FROM app.jpanel_message
                WHERE sender_kind = 'panel' AND recipient_kind = 'owner'
                  AND played_at IS NULL
                GROUP BY sender_device
                """
            )
        )
    ).all()
    return {str(pid): int(n) for pid, n in rows}


def _name_of(names: dict[str, str], kind: str, device: str | None) -> str:
    return DAD_NAME if kind == "owner" else names.get(device or "", "the other one")


def _row_to_message(row, names: dict[str, str]) -> Message:
    (mid, s_kind, s_dev, r_kind, r_dev, transcript, composed, dur, created, played) = row
    return Message(
        id=str(mid),
        from_name=_name_of(names, s_kind, s_dev),
        to_name=_name_of(names, r_kind, r_dev),
        # Relative to the OWNER, who is the only reader that needs a direction at all: a panel
        # sees nothing but its own inbox, so every row it can read is incoming by definition.
        direction="in" if s_kind == "panel" else "out",
        transcript=transcript or "",
        composed=composed,
        duration_ms=int(dur or 0),
        created_at=created.isoformat(),
        played_at=played.isoformat() if played else None,
    )


# --- the panel's four routes ----------------------------------------------------------------


@router.post("/send")
async def send(
    principal: PanelDep,
    request: Request,
    to: Literal["panel", "dad"] = Query(...),
) -> SendResult:
    """A child records a message and it is filed for someone else.

    RAW 16 kHz MONO s16 IN THE BODY, exactly as `/converse` takes it — no container, no codec,
    because the panel has neither. The first cut of the contract said multipart; a panel that
    cannot build a multipart body cannot send a message, and growing a MIME encoder in the
    firmware to satisfy a table in a plan would have been the wrong way round.

    Transcribed on the way in, because the owner reads before he listens. A failed
    transcription is NOT a failed send: the audio is the message, the text is a convenience,
    and refusing to deliver a child's voice because whisper was busy would be the wrong
    trade."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="no audio")
    audio = audio[:MAX_MESSAGE_BYTES]
    audio = _trim_to_speech(audio)
    if not audio:
        # Silence is not an error; it is a child who pressed and said nothing.
        raise HTTPException(status_code=400, detail="nothing said")
    duration_ms = len(audio) * 1000 // (PANEL_RATE * 2)

    settings = cast(Settings, request.app.state.settings)
    ctx = ctx_for(principal)

    # NO PANEL-SCOPED SESSION HERE ANY MORE. Resolving the recipient was wrapped in one, which
    # is what hid the RLS problem: it looked like the panel was reading the roster, and a panel
    # cannot. Addressing is `_panel_names`'s own business now (see its note), and this block
    # touches no other table, so the session it used to open had nothing left to do.
    names = await _panel_names(request.app.state.session_maker)
    if to == "dad":
        r_kind, r_dev = "owner", None
    else:
        # BY NAME, NOT BY ID, and the difference is a panel talking to itself.
        #
        # `_panel_names` keeps the NEWEST key per name, so a panel still running an older
        # key for its own name is not in that dict under its own id — filtering on
        # `pid != principal.id` would leave its own name in the list and post the child's
        # message straight back to the unit they spoke into. Which panel a key belongs to
        # is the name, not the row.
        me = _display_name(principal.label)
        others = [pid for pid, name in names.items() if name != me]
        if len(others) != 1:
            # THE RULE THE PLAN REFUSED TO GUESS AT. A toy that silently posts to the wrong
            # sibling is worse than one that says it cannot, so this is a refusal the panel
            # speaks aloud rather than a best guess.
            log.info("jpanel.no_single_other_panel", me=me, candidates=len(others))
            raise HTTPException(status_code=409, detail="no single other panel")
        r_kind, r_dev = "panel", others[0]

    # TWO SESSIONS, WITH THE SLOW WORK BETWEEN THEM. Whisper and the blob write are network
    # calls measured in seconds; holding a scoped database session open across them would pin a
    # connection for the length of a transcription on a box that also serves the pet's turns.
    transcript = await _transcribe(settings, audio)
    sha = await request.app.state.blob_store.put(_wav(audio))

    async with scoped_session(request.app.state.session_maker, ctx) as session:
        mid = (
            await session.execute(
                text(
                    """
                    INSERT INTO app.jpanel_message
                        (sender_kind, sender_device, recipient_kind, recipient_device,
                         blob_sha256, transcript, composed, duration_ms)
                    VALUES ('panel', :me, :rk, :rd, :sha, :tx, 'voice', :ms)
                    RETURNING id::text
                    """
                ),
                {
                    "me": principal.id,
                    "rk": r_kind,
                    "rd": r_dev,
                    "sha": sha,
                    "tx": transcript,
                    "ms": duration_ms,
                },
            )
        ).scalar_one()
        await session.commit()

    to_name = DAD_NAME if to == "dad" else names.get(r_dev or "", "the other one")
    log.info("jpanel.sent", to=to_name, duration_ms=duration_ms, transcript=transcript[:120])
    return SendResult(id=str(mid), to_name=to_name)


async def _transcribe(settings: Settings, audio: bytes) -> str:
    """Best-effort. The audio is the message; the text is a convenience for the reader."""
    if not settings.whisper_url:
        return ""
    try:
        client = WhisperCppClient(
            base_url=settings.whisper_url,
            model=settings.whisper_model,
            timeout=min(settings.whisper_timeout, 60.0),
        )
        seconds = len(audio) / (PANEL_RATE * 2)
        result = await client.transcribe(
            _wav(audio),
            filename="jpanel.wav",
            media_type="audio/wav",
            audio_ctx=max(160, min(1500, int(seconds * 50 * 1.5))),
            language="en",
        )
        return (result.text or "").strip()
    except Exception as exc:  # noqa: BLE001 — a mute transcript never costs a delivery
        log.warning("jpanel.stt_failed", error=repr(exc))
        return ""


@router.get("/waiting")
async def waiting(principal: PanelDep, request: Request) -> Waiting:
    """Is anything here for me. Polled every ~30 s per panel, so it stays small."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*),
                           min(created_at)
                    FROM app.jpanel_message
                    WHERE recipient_device = :me AND played_at IS NULL
                    """
                ),
                {"me": principal.id},
            )
        ).first()
        count = int(row[0]) if row else 0
        from_name = ""
        if count:
            names = await _panel_names(request.app.state.session_maker)
            oldest = (
                await session.execute(
                    text(
                        """
                        SELECT sender_kind, sender_device
                        FROM app.jpanel_message
                        WHERE recipient_device = :me AND played_at IS NULL
                        ORDER BY created_at LIMIT 1
                        """
                    ),
                    {"me": principal.id},
                )
            ).first()
            if oldest:
                from_name = _name_of(names, oldest[0], oldest[1])
    return Waiting(count=count, from_name=from_name)


@router.get("/next")
async def next_message(principal: PanelDep, request: Request) -> Response:
    """The oldest unplayed message, as raw PCM the panel can hand straight to its speaker.

    DOES NOT MARK IT PLAYED — `POST /played` does, once the panel has actually finished. That
    separation is what makes a message survive a reboot mid-playback rather than being lost by
    having been handed over."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text, blob_sha256, sender_kind, sender_device
                    FROM app.jpanel_message
                    WHERE recipient_device = :me AND played_at IS NULL
                    ORDER BY created_at LIMIT 1
                    """
                ),
                {"me": principal.id},
            )
        ).first()
        if row is None:
            return Response(status_code=204)
        names = await _panel_names(request.app.state.session_maker)

    wav = await request.app.state.blob_store.get(row[1])
    pcm, rate = _pcm_from_wav(wav)
    return Response(
        content=_to_panel_rate(pcm, rate),
        media_type="application/octet-stream",
        headers={"X-Jpanel-Id": row[0], "X-Jpanel-From": _name_of(names, row[2], row[3])},
    )


class Played(BaseModel):
    id: str


@router.post("/played", status_code=204)
async def played(principal: PanelDep, request: Request, body: Played) -> Response:
    """The child heard it. Bounded to this panel's own inbox by policy, not by this handler."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as session:
        await session.execute(
            text(
                "UPDATE app.jpanel_message SET played_at = now()"
                " WHERE id = CAST(:id AS uuid) AND played_at IS NULL"
            ),
            {"id": body.id},
        )
        await session.commit()
    return Response(status_code=204)


# --- the owner's three routes ---------------------------------------------------------------


@router.get("/messages")
async def messages(owner: OwnerDep, request: Request, limit: int = 100) -> Threads:
    """Everything, grouped by the panel it concerns — which is the question asked at work.

    `limit` bounds the ROWS RETURNED across all panels, not the unplayed counts: those come
    from `_unplayed_by_panel` precisely so a truncating limit cannot deflate a badge.

    EVERY ENROLLED PANEL IS LISTED, including one that has never sent anything. Otherwise the
    owner could not message a twin who has not yet spoken into her panel — exactly the child he
    would most want to reach."""
    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        names = await _panel_names(request.app.state.session_maker)
        unplayed = await _unplayed_by_panel(session)
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, sender_kind, sender_device, recipient_kind, recipient_device,
                           transcript, composed, duration_ms, created_at, played_at
                    FROM app.jpanel_message
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                {"lim": max(1, min(limit, 500))},
            )
        ).all()

    threads: dict[str, PanelThread] = {
        pid: PanelThread(device_id=pid, name=name, unplayed=unplayed.get(pid, 0), messages=[])
        for pid, name in names.items()
    }
    for row in rows:
        # A row belongs to the thread of whichever end is a PANEL — the owner is the other end
        # of every conversation he can see, so grouping by him would make one undifferentiated
        # list and lose the only axis he cares about.
        pid = row[2] if row[1] == "panel" else row[4]
        thread = threads.get(str(pid or ""))
        if thread is None:
            continue
        thread.messages.append(_row_to_message(row, names))
    return Threads(panels=list(threads.values()))


@router.post("/messages", status_code=201)
async def send_text(owner: OwnerDep, request: Request, body: SendText) -> Message:
    """Dad types; the panel hears it spoken.

    The typed text is kept as the transcript so both ends agree about what was said — the child
    hears it and the owner's own list shows exactly what he sent, rather than a re-transcription
    of speech he never made."""
    settings = cast(Settings, request.app.state.settings)
    base = (settings.brain_tts_url or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="speech synthesis not configured")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(
                f"{base}/tts", params={"text": body.text[:600], "voice": DAD_VOICE}
            )
        resp.raise_for_status()
        wav_pcm, wav_rate = _pcm_from_wav(resp.content)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("jpanel.tts_failed", error=repr(exc))
        raise HTTPException(status_code=503, detail="could not speak") from exc

    pcm = _trim_to_speech(_to_panel_rate(wav_pcm, wav_rate), lead_ms=30, tail_ms=120)
    duration_ms = len(pcm) * 1000 // (PANEL_RATE * 2)
    sha = await request.app.state.blob_store.put(_wav(pcm))

    ctx = ctx_for(owner)
    async with scoped_session(request.app.state.session_maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO app.jpanel_message
                        (sender_kind, recipient_kind, recipient_device, blob_sha256,
                         transcript, composed, duration_ms)
                    VALUES ('owner', 'panel', :to, :sha, :tx, 'text', :ms)
                    RETURNING id, sender_kind, sender_device, recipient_kind, recipient_device,
                              transcript, composed, duration_ms, created_at, played_at
                    """
                ),
                {"to": body.to_device, "sha": sha, "tx": body.text, "ms": duration_ms},
            )
        ).first()
        await session.commit()
        names = await _panel_names(request.app.state.session_maker)
    if row is None:  # pragma: no cover — RETURNING on a successful INSERT always yields
        raise HTTPException(status_code=500, detail="message not stored")
    log.info(
        "jpanel.spoke",
        to=names.get(body.to_device, body.to_device),
        tts_ms=int((time.monotonic() - started) * 1000),
        duration_ms=duration_ms,
    )
    return _row_to_message(row, names)


@router.post("/messages/{message_id}/played", status_code=204)
async def mark_read(owner: OwnerDep, request: Request, message_id: str) -> Response:
    """The owner has dealt with this one.

    THE BADGE HAD NO WAY TO CLEAR WITHOUT THIS, found by building the PWA against the contract:
    the panel side had `POST /played` and the owner side had nothing, so `PanelThread.unplayed`
    could only ever climb.

    Stamping it in `GET .../audio` instead would have been the tempting fix and is the wrong
    one. §3b's premise is that the TRANSCRIPT is the primary content — the expected interaction
    is reading, not playing — so a father who reads the text and never presses play would leave
    the count sitting there forever. Clearing has to be something the reader can do by reading.

    Idempotent, and `played_at IS NULL` keeps the FIRST time: when he saw it is a real answer,
    and a second look should not overwrite it."""
    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        await session.execute(
            text(
                "UPDATE app.jpanel_message SET played_at = now()"
                " WHERE id = CAST(:id AS uuid) AND played_at IS NULL"
            ),
            {"id": message_id},
        )
        await session.commit()
    return Response(status_code=204)


@router.get("/messages/{message_id}/audio")
async def message_audio(owner: OwnerDep, request: Request, message_id: str) -> Response:
    """The fallback for when the transcript does not make sense — which, given how the
    transcriber handles four-year-olds, is often."""
    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        row = (
            await session.execute(
                text("SELECT blob_sha256 FROM app.jpanel_message WHERE id = CAST(:id AS uuid)"),
                {"id": message_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="no such message")
    return Response(content=await request.app.state.blob_store.get(row[0]), media_type="audio/wav")
