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

import hashlib
import string
import time
import uuid
from typing import Literal, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from jbrain.api.deps import OwnerDep, PanelDep
from jbrain.api.endpoint import (
    PANEL_RATE,
    UNNAMED_PANEL_LABEL,
    _pcm_from_wav,
    _to_panel_rate,
    _trim_to_speech,
    _wav,
    panel_display_name,
)
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.transcribe import WhisperCppClient

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/jpanel", tags=["jpanel"])

# A panel's clip: longer than a conversational turn, because a message is a thought rather
# than an answer, and short enough that a pocket-dial cannot fill the box.
MAX_MESSAGE_MS = 30_000
MAX_MESSAGE_BYTES = PANEL_RATE * 2 * MAX_MESSAGE_MS // 1000

# DAD'S VOICE, AND THE `kokoro-` PREFIX IS NOT DECORATION.
#
# This said "am_michael" and every message from the owner arrived in the PET'S voice, which is
# the exact thing a separate voice exists to prevent: a message from Dad in the robot's voice
# teaches a four-year-old that the robot and their father are the same thing.
#
# `_resolve_kokoro_voice` in `deploy/tts-stt/tts_server.py` returns the DEFAULT for any id that
# does not start with `kokoro-`, and the default is `CURATED_KOKORO_VOICES[0]` — af_heart, the
# pet's own voice. That fallback is deliberate on its side (a stale id from an old client should
# render rather than error) and it is exactly why this was silent: the box logged a successful
# render, the panel played perfectly good speech, and nothing anywhere said the voice had been
# swapped. It took the owner hearing it.
#
# Pinned by `test_dads_voice_is_one_the_engine_will_actually_use`, which reads the resolver's own
# rule and roster out of that file rather than trusting this string.
DAD_VOICE = "kokoro-am_michael"
DAD_NAME = "Dad"

# HOW MANY TIMES THE BOX WILL HAND THE SAME MESSAGE TO THE SAME PANEL BEFORE GIVING UP.
#
# A panel that cannot acknowledge must not be able to loop audio in a child's bedroom, and that
# is the box's job because the box is the half that can be fixed without an OTA — §10.4cw is
# the afternoon this was learned the hard way.
#
# Five, because the honest failures are all ONE: a dropped POST, a crash mid-playback, a power
# cut between hearing and acknowledging. Retrying a handful of times covers every one of them
# with room to spare, and the sixth identical delivery is not a flaky link, it is a panel that
# cannot tell us it heard.
JPANEL_MAX_DELIVERIES = 5


class SendResult(BaseModel):
    id: str
    to_name: str


class Waiting(BaseModel):
    """What the panel polls for. Deliberately tiny — it runs every ~30 s per panel forever."""

    count: int = 0
    from_name: str = ""
    # WHO THE OTHER PANEL IS, so the blue recording indicator can say TO ELORA rather than
    # MESSAGE. A panel had no way to ask that — there was no route for it, and inventing a word
    # for a child's twin would be worse than the placeholder — so the gap was written into
    # `display.c` as an honest one. This is the answer, and it rides the poll the panel already
    # makes rather than adding a second one.
    #
    # Empty unless there is EXACTLY ONE other panel, which is the same rule `send(to="panel")`
    # already enforces: with two siblings "the other one" is not a name, it is a question, and
    # a label that guesses would put the wrong child on the glass.
    sibling: str = ""


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
    # HOW MANY TIMES THE BOX HANDED THIS TO A PANEL. On the wire so the PWA can tell a message
    # that is merely waiting from one the box has GIVEN UP delivering — the two are identical
    # in `played_at` and only one of them means something is wrong.
    deliveries: int = 0
    undelivered: bool = False


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
#
# IMPORTED RATHER THAN RESTATED, and that is the fix for the fault above. The label is written
# in `endpoint.py` (`/flash`) and matched here, and for a while the only thing holding the two
# together was a test that read one module's source from the other's. One definition cannot
# come apart; a test that two strings agree can only notice after they have.
_UNNAMED_LABEL = UNNAMED_PANEL_LABEL
_display_name = panel_display_name


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
    (mid, s_kind, s_dev, r_kind, r_dev, transcript, composed, dur, created, played, deliveries) = (
        row
    )
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
        deliveries=int(deliveries or 0),
        # Unplayed AND out of attempts. Computed here rather than in the PWA so one definition
        # of "gave up" exists, on the side that owns the cap.
        undelivered=played is None and int(deliveries or 0) >= JPANEL_MAX_DELIVERIES,
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
    trade.

    **VERIFIED, NOT ASSUMED.** The panel hashes the recording before it sends and puts the
    digest in `X-Jpanel-Sha256`; this compares it against what actually arrived. The upload is
    a chunked write over a radio in a bedroom, and a connection that ends cleanly two thirds of
    the way through a sentence is indistinguishable, from here, from a child who stopped
    talking — so without this the box would store the fragment, whisper would transcribe it,
    and she would be told her message went. A mismatch is a 422 and the panel sends the same
    buffer again, which is the right answer because the buffer is the good copy.

    Unverified uploads are still accepted, with a log line. A panel on older firmware sends no
    header, and refusing it would take voice post away from a unit mid-fleet-upgrade to fix a
    fault it does not have."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="no audio")
    if len(audio) > MAX_MESSAGE_BYTES:
        # REFUSED RATHER THAN TRUNCATED. This used to silently keep the first N bytes, which is
        # the same fault the hash exists to catch, committed deliberately: a child's message
        # stored with its end cut off and nothing anywhere saying so.
        raise HTTPException(
            status_code=413,
            detail=f"message longer than {MAX_MESSAGE_MS // 1000}s",
        )
    claimed = request.headers.get("X-Jpanel-Sha256", "").strip().lower()
    if claimed:
        actual = hashlib.sha256(audio).hexdigest()
        if actual != claimed:
            log.warning(
                "jpanel.upload_corrupt",
                panel=principal.id,
                bytes=len(audio),
                claimed=claimed,
                actual=actual,
            )
            raise HTTPException(status_code=422, detail="audio did not survive the upload")
    else:
        log.info("jpanel.upload_unverified", panel=principal.id, bytes=len(audio))
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
        # ONCE PER POLL, AND UNCONDITIONALLY. It used to be read only when something was
        # waiting; the indicator it now also feeds is drawn while a child is RECORDING, which
        # is precisely the case where nothing is. Two small queries twice a minute.
        names = await _panel_names(request.app.state.session_maker)
        from_name = ""
        if count:
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
        me = _display_name(principal.label)
        others = [n for pid, n in names.items() if pid != str(principal.id) and n != me]
        sibling = others[0] if len(others) == 1 else ""
    return Waiting(count=count, from_name=from_name, sibling=sibling)


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
                    SELECT id::text, blob_sha256, sender_kind, sender_device, deliveries
                    FROM app.jpanel_message
                    WHERE recipient_device = :me AND played_at IS NULL
                      AND deliveries < :cap
                    ORDER BY created_at LIMIT 1
                    """
                ),
                {"me": principal.id, "cap": JPANEL_MAX_DELIVERIES},
            )
        ).first()
        if row is None:
            return Response(status_code=204)
        # COUNTED BEFORE IT IS SENT, not after it is acknowledged — the whole point is to bound
        # deliveries that are never acknowledged, so a count that only moved on success would
        # never move at all in the case this exists for.
        await session.execute(
            text(
                "UPDATE app.jpanel_message SET deliveries = deliveries + 1"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": row[0]},
        )
        await session.commit()
        if int(row[4]) + 1 >= JPANEL_MAX_DELIVERIES:
            # LOUD, because this is the box giving up on delivering a child's message and the
            # only other symptom is silence. The row stays UNPLAYED — it was never heard — and
            # `deliveries` goes out on the wire so the PWA can say so rather than showing it as
            # merely waiting.
            log.warning(
                "jpanel.delivery_gave_up",
                message=row[0],
                panel=principal.id,
                deliveries=int(row[4]) + 1,
            )
        names = await _panel_names(request.app.state.session_maker)

    wav = await request.app.state.blob_store.get(row[1])
    pcm, rate = _pcm_from_wav(wav)
    body = _to_panel_rate(pcm, rate)
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={
            "X-Jpanel-Id": row[0],
            "X-Jpanel-From": _name_of(names, row[2], row[3]),
            # THE OTHER HALF OF THE PROOF. The panel streams this straight into its speaker and
            # discards it as it plays, so a download that ends early is a message that stops
            # mid-sentence — and the panel would then acknowledge it and the box would never
            # offer it again. With the digest it can hash as it plays and simply not
            # acknowledge what it could not verify, which leaves the message unplayed and the
            # pop-up standing.
            "X-Jpanel-Sha256": hashlib.sha256(body).hexdigest(),
        },
    )


@router.get("/message/{message_id}/pcm")
async def message_pcm(message_id: str, principal: PanelDep, request: Request) -> Response:
    """One message's audio by id, for a panel that has already been given it.

    **THIS EXISTS BECAUSE THE PANEL NO LONGER KEEPS THE BYTES.** It used to hold a whole
    message in PSRAM, so the repeat icon — *tap to hear it again* — replayed from memory. Since
    0.2.96 the audio streams through a four-second ring and is gone as it plays, which is what
    lifts the length cap; the cost is that "again" has to ask the box a second time.

    **It does NOT touch `deliveries`.** That counter is the give-up rule: five attempts to hand
    a message over and the box stops trying (`JPANEL_MAX_DELIVERIES`). A replay is not an
    attempt to deliver — the child has already heard it and is asking for it again — and
    counting it would make listening twice a way to lose a message. For the same reason this
    route does not care whether the row is played: by definition it is.

    Isolation is the table's, not this handler's. `jpanel_message_panel_read` opens a row only
    to the panel that sent it or was sent it, so a panel guessing another twin's message id
    gets a 404 from the policy rather than from a check written here (migration 0208)."""
    try:
        # A path segment that is not a uuid would reach the cast below and come back as a 500,
        # which reads as the box being broken rather than as a message that is not there.
        uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such message") from None
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT blob_sha256 FROM app.jpanel_message
                    WHERE id = CAST(:id AS uuid) AND recipient_device = :me
                    """
                ),
                {"id": message_id, "me": principal.id},
            )
        ).first()
    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail="no such message")

    wav = await request.app.state.blob_store.get(str(row[0]))
    pcm, rate = _pcm_from_wav(wav)
    body = _to_panel_rate(pcm, rate)
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={
            "X-Jpanel-Id": message_id,
            "X-Jpanel-Sha256": hashlib.sha256(body).hexdigest(),
        },
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
                           transcript, composed, duration_ms, created_at, played_at, deliveries
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


# THE PANEL'S FONT IS THE REAL CONSTRAINT ON A NAME, not taste and not the column width.
#
# `font.c` carries 5x7 cells for A-Z, the digits, space, hyphen and full stop — and nothing
# else, uppercase only, because at that size a lowercase set is a second alphabet with
# descenders to place. A character it does not have draws as NOTHING, so a name with an
# apostrophe in it would reach a four-year-old as a pop-up from someone whose name is missing a
# letter. Rejected at the door instead, where the owner is standing and can retype it.
_PANEL_NAME_CHARS = frozenset(string.ascii_uppercase + string.digits + " -.")

# Fourteen, and the number is arithmetic rather than a guess. The pop-up's bubble is 296 px
# wide with 32 px of padding, and `font_text_w` is `(n * 5 + (n - 1)) * scale`: at the shrunk
# scale of 3 that leaves 14 characters, and `draw_popup` shrinks rather than clips precisely so
# a long name still names somebody. Eleven or fewer renders at the full scale of 4.
MAX_PANEL_NAME = 14


def _panel_name(raw: str) -> str:
    """The typed name, normalised, or a 422 that says what is wrong with it.

    Pure and separate from the route because it is the half that can be tested without a
    database — and because it is the half coupled to something two packages away: the panel's
    5x7 font."""
    name = " ".join(raw.split())
    if not name or any(c not in _PANEL_NAME_CHARS for c in name.upper()):
        raise HTTPException(
            status_code=422,
            detail="a panel name may use letters, digits, spaces, hyphens and full stops only",
        )
    if name.lower() == _display_name(_UNNAMED_LABEL):
        # The name an UNNAMED panel already answers to. Typed as a real name it would produce
        # two panels the PWA and the pop-up both call "the other one", which is the one
        # ambiguity this whole route exists to remove.
        raise HTTPException(status_code=409, detail="that is what an unnamed panel is called")
    return name


class RenamePanel(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_PANEL_NAME)


class Renamed(BaseModel):
    device_id: str
    name: str
    # HOW MANY KEYS MOVED, because the answer is routinely not one and the owner should see
    # that rather than wonder. Every `/flash` mints a fresh device key and nothing retires the
    # old one, so a panel flashed four times is four principals carrying one label.
    keys: int


@router.post("/panels/{device_id}/name")
async def rename_panel(
    device_id: str, owner: OwnerDep, request: Request, body: RenamePanel
) -> Renamed:
    """Name a panel, from the PWA, with no cable and no re-flash.

    **The name a panel is known by lives on the BOX, not in its firmware.** `/flash` writes
    `panel <name>` onto the device key it mints, and everything the twins actually see —
    the thread in the PWA, the `X-Jpanel-From` a sibling's pop-up reads out — comes from that
    label. So a panel enrolled without a name announces itself as "the other one" forever, and
    until this route the only way to correct it was to re-flash a unit over USB. That is a
    terminal by another name, which CLAUDE.md #10 exists to stop.

    **EVERY KEY UNDER THE OLD LABEL MOVES, AND THAT IS THE WHOLE DESIGN.** `_panel_names`
    collapses the roster with `DISTINCT ON (label)` because a re-flashed panel leaves its dead
    keys behind — the label IS the identity in this model. Renaming only the newest key would
    therefore leave the older ones sitting under the old name, and the roster would grow a
    second panel: a "the other one" thread pointing at a principal nothing can reach, next to
    the freshly-named one. Moving the group keeps the collapse true.

    **`{device_id}` IS THE PANEL'S PRINCIPAL ID — the one `GET /messages` puts on a thread —
    and NOT the subject id `POST /api/devices` echoes back.** The two id spaces both exist
    here: a device's stable identity is its subject, while a panel is addressed, stored against
    and collapsed by its PRINCIPAL, because that is what mints a key and carries the label. So
    this route addresses principals, and `/api/devices/{id}/rename` — which takes the subject —
    is a different control on a different thing despite the identical name. Renaming a panel
    through that one moves one device's label and leaves the roster alone.

    Refused when the name is already another panel's, because `_panel_names` keeps one row per
    label: two panels called Elora become one row and one twin becomes unreachable. That is the
    one failure this addressing model has, it is documented where it is caused, and it must not
    be reachable from a text box."""
    name = _panel_name(body.name)
    label = f"panel {name}"
    try:
        # Checked here rather than left to the cast below: a path segment that is not a uuid
        # would reach Postgres and come back as a 500, which reads as the box being broken
        # rather than as a link that has gone stale.
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None

    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        current = (
            await session.execute(
                text(
                    """
                    SELECT label FROM app.principals
                    WHERE id = CAST(:dev AS uuid) AND kind = 'device_key'
                      AND revoked_at IS NULL
                      AND (label LIKE 'panel%' OR label = :unnamed)
                    """
                ),
                {"dev": device_id, "unnamed": _UNNAMED_LABEL},
            )
        ).scalar_one_or_none()
        if current is None:
            raise HTTPException(status_code=404, detail="no such panel")
        taken = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM app.principals
                    WHERE kind = 'device_key' AND revoked_at IS NULL
                      AND lower(label) = lower(:label) AND label <> :current
                    """
                ),
                {"label": label, "current": str(current)},
            )
        ).scalar_one()
        if int(taken) > 0:
            raise HTTPException(
                status_code=409,
                detail=f"another panel is already called {name} — two panels with one name "
                "collapse to one and a twin becomes unreachable",
            )
        moved = len(
            (
                await session.execute(
                    text(
                        """
                        UPDATE app.principals SET label = :label
                        WHERE kind = 'device_key' AND revoked_at IS NULL AND label = :current
                        RETURNING 1
                        """
                    ),
                    {"label": label, "current": str(current)},
                )
            ).all()
        )
        await session.commit()
    log.info("jpanel.panel_renamed", device=device_id, was=str(current), now=label, keys=moved)
    return Renamed(device_id=device_id, name=_display_name(label), keys=int(moved))


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
                              transcript, composed, duration_ms, created_at, played_at, deliveries
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


class Cleared(BaseModel):
    deleted: int
    kept: int


@router.delete("/messages")
async def clear_history(owner: OwnerDep, request: Request, device: str = Query(...)) -> Cleared:
    """Clear one panel's conversation.

    Deletes exactly the rows that panel's thread SHOWS — what it sent (to the owner or to its
    sibling) and what the owner sent to it — because a button under a conversation that cleared
    something else would be a button nobody could predict.

    **EXCEPT A MESSAGE A CHILD HAS NOT HEARD YET, and that exception is not a nicety.**
    `JPANEL_PLAN.md` §5: *"unplayed messages are kept indefinitely — a message nobody heard is
    the one thing that must not evaporate."* A row addressed to a panel with `played_at IS NULL`
    is a message sitting on a wall waiting for a four-year-old to come back to it; deleting it
    means she never hears it and nobody ever knows it existed. The owner clearing his own view
    is not a decision about her post.

    A panel's unread message to the OWNER is a different thing and is deleted: that is his own
    badge, he is looking at the thread, and clearing is exactly the call he is making.

    The count of what was kept comes back so the PWA can SAY so. A clear that silently leaves
    rows behind is worse than one that refuses — the whole point of the button is that the list
    afterwards matches what he expects."""
    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        row = (
            await session.execute(
                text(
                    """
                    WITH mine AS (
                        SELECT id, recipient_kind, played_at
                        FROM app.jpanel_message
                        WHERE (sender_kind = 'panel' AND sender_device = :dev)
                           OR (sender_kind = 'owner' AND recipient_device = :dev)
                    ), gone AS (
                        DELETE FROM app.jpanel_message
                        WHERE id IN (
                            SELECT id FROM mine
                            WHERE NOT (recipient_kind = 'panel' AND played_at IS NULL)
                        )
                        RETURNING 1
                    )
                    SELECT (SELECT count(*) FROM gone),
                           (SELECT count(*) FROM mine
                            WHERE recipient_kind = 'panel' AND played_at IS NULL)
                    """
                ),
                {"dev": device},
            )
        ).first()
        await session.commit()
    deleted, kept = (int(row[0]), int(row[1])) if row else (0, 0)
    log.info("jpanel.history_cleared", device=device, deleted=deleted, kept=kept)
    return Cleared(deleted=deleted, kept=kept)


@router.post("/messages/audio", status_code=201)
async def send_audio(
    owner: OwnerDep,
    request: Request,
    to_device: str = Query(...),
) -> Message:
    """Dad SPEAKS; the panel plays his actual voice.

    The owner, after the typed path shipped: *"PWA should also be able to actually send audio,
    a voice message, that have the option to send text that gets rendered."* Typing is now one
    of two ways rather than the only one, and `JPANEL_PLAN.md` §3b's asymmetry is amended to
    match — the PWA still never has to LISTEN, but it may speak.

    **THE REASON THIS IS WORTH THE ROUTE**: a synthesised voice reading a father's words is not
    the same object as his voice. The whole design already turns on that — `DAD_VOICE` exists
    because a message from Dad arriving in the pet's own voice would teach a four-year-old that
    the robot and their father are the same thing. A real recording settles the question
    completely, and for a child who cannot read it is the only version that carries who it is
    from.

    **NO FIRMWARE CHANGE, AND THAT IS NOT LUCK.** `GET /next` hands the panel raw PCM and the
    panel plays it; nothing in the firmware knows or cares whether that audio came from a
    microphone, from Kokoro, or from a phone in an office. The contract was drawn at the right
    seam, so this lands entirely on the box and the PWA.

    **RAW 16 kHz MONO s16 IN THE BODY, exactly as the panel's `/send` takes it**, and the
    browser does the conversion. A `MediaRecorder` blob is webm/opus or mp4/aac depending on
    the browser, and decoding that on the box would mean a codec dependency in the api
    container for a job the recorder's own browser can already do — every browser can decode
    what it just recorded. One audio format crosses this boundary, the same one the panels
    speak, and the PWA resamples before it uploads.

    **Transcribed on the way in, and the transcript may be WRONG.** Unlike the typed path — where
    the text IS what was said and is kept verbatim — this is a re-transcription of real speech,
    with all of whisper's failings. That is acceptable for the same reason it is on the panel's
    side: the audio is the message and the text is a convenience. A failed transcription is not
    a failed send."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="no audio")
    audio = audio[:MAX_MESSAGE_BYTES]
    audio = _trim_to_speech(audio)
    if not audio:
        # The owner pressed record and said nothing, or held the wrong microphone. Refused
        # rather than filed: an empty message would draw a pop-up on a child's wall for silence.
        raise HTTPException(status_code=400, detail="nothing said")
    duration_ms = len(audio) * 1000 // (PANEL_RATE * 2)

    settings = cast(Settings, request.app.state.settings)
    # The addressee is checked BEFORE the slow work, so a bad device id fails in milliseconds
    # rather than after a transcription. `_panel_names` is the roster — one row per name — so
    # this also rejects a superseded key the PWA might still be holding from a stale render.
    names = await _panel_names(request.app.state.session_maker)
    if to_device not in names:
        raise HTTPException(status_code=404, detail="no such panel")

    # TWO SESSIONS WITH THE SLOW WORK BETWEEN THEM, for the reason `send` gives: whisper and
    # the blob write are network calls measured in seconds, and holding a scoped session across
    # them pins a connection on a box that also serves the pet's turns.
    transcript = await _transcribe(settings, audio)
    sha = await request.app.state.blob_store.put(_wav(audio))

    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO app.jpanel_message
                        (sender_kind, recipient_kind, recipient_device, blob_sha256,
                         transcript, composed, duration_ms)
                    VALUES ('owner', 'panel', :to, :sha, :tx, 'voice', :ms)
                    RETURNING id, sender_kind, sender_device, recipient_kind, recipient_device,
                              transcript, composed, duration_ms, created_at, played_at, deliveries
                    """
                ),
                {"to": to_device, "sha": sha, "tx": transcript, "ms": duration_ms},
            )
        ).first()
        await session.commit()
    if row is None:  # pragma: no cover — RETURNING on a successful INSERT always yields
        raise HTTPException(status_code=500, detail="message not stored")
    log.info(
        "jpanel.spoke_aloud",
        to=names.get(to_device, to_device),
        duration_ms=duration_ms,
        transcribed=bool(transcript),
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
