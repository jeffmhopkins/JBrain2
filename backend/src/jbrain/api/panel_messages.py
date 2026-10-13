"""Voice post on the owner's desk panel: his conversations with the twins' panels.

docs/plans/JPANEL_C6_PLAN.md §1 "Voice messages with the twins" (firmware-c6 0.0.16). The owner,
2026-10-12: *"Am I able to receive voice messages now from the kids' other jpanel?"* A twin's
panel sends "to Dad" through `jpanel.send`, which files an owner-addressed row; until this, only
the PWA read those rows. The desk panel is now a second owner client for the same rows, beside
the PWA, and every route here is a thin door onto `api/jpanel.py`'s own functions: the same
thread query, the same played stamp, the same PCM body, the same filing of Dad's voice. Nothing
here decides what a message is.

THE GATE IS THE TALK'S (`panel_convo.owner_for_panel` → `panel_agent.agent_allowed`): a panel
flashed as a `display` that the owner switched on. That switch already lets the panel act AS the
owner with his full agent, which is strictly more than reading and answering his post; a second
flag would be a second thing to get right, a PWA toggle and a migration for a combination
("agent yes, post no") nobody has asked for. The twins' `jpet` panels are refused here exactly as
there — they reach their own post through `jpanel.py`'s panel routes and nothing else.

What the panel acts on is the OWNER's conversation with one twin, on owner-scoped sessions
(#3): the rows she sent him and the rows he sent her. Twin-to-twin post is not his conversation
and is never shown, played or stamped here. `jpanel_message` carries no health, finance or
location domain; nothing else is read.

THE NEW-MESSAGE SIGNAL rides the debug pickup the panel already makes every ~3 s: `X-Jpanel-Post`
is a short tag of the owner's post (`post_tag`), and the panel fetches its threads only when it
moves. An idle panel costs one aggregate query a pickup, no extra request.
"""

from __future__ import annotations

import hashlib
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api import jpanel
from jbrain.api.deps import PanelDep
from jbrain.api.panel_agent import agent_allowed, screen_text
from jbrain.api.panel_convo import owner_for_panel
from jbrain.auth.owner import owner_principal_id
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, scoped_session

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint/messages", tags=["endpoint"])

#: A bubble keeps 255 bytes (firmware `msg_t.text`); the panel keeps four messages a person
#: (firmware `PERSON_KEEP`) and may ask for up to eight.
TEXT_BYTES = 255
THREAD_DEFAULT = 4
THREAD_MAX = 8
#: `X-Jpanel-Sha256: trailer` — the panel streams its recording as it is made, so it cannot know
#: the digest when the headers go. It appends the 32 raw bytes of sha256(audio) after the audio
#: instead, and the box checks them exactly as `jpanel.send` checks the header.
TRAILER = "trailer"
DIGEST_BYTES = 32
#: The pickup's header and the length of its tag.
POST_HEADER = "X-Jpanel-Post"
TAG_HEX = 8


class PostLine(BaseModel):
    id: str
    #: The owner's own (right), or hers (left, with play).
    mine: bool
    #: The transcript (or what he typed), cut to a bubble on a word; "" when there is none.
    text: str
    ms: int
    #: Hers: the owner has played it (here or in the PWA). His: she has.
    played: bool
    voice: bool


class PostThread(BaseModel):
    device_id: str
    name: str
    #: The same tag the pickup carries, as of this answer.
    tag: str
    messages: list[PostLine]


class Sent(BaseModel):
    id: str
    text: str
    ms: int


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


async def post_tag(maker: async_sessionmaker[AsyncSession], owner: SessionContext) -> str:
    """A short tag that moves whenever the owner's post does: a message to or from him filed,
    played or cleared. Equal tags mean nothing the desk panel shows has changed."""
    async with scoped_session(maker, owner) as s:
        result = await s.execute(
            text(
                """
                SELECT count(*), max(created_at)::text, max(played_at)::text,
                       count(*) FILTER (WHERE played_at IS NULL)
                FROM app.jpanel_message
                WHERE sender_kind = 'owner' OR recipient_kind = 'owner'
                """
            )
        )
        row = result.one()
    return hashlib.sha256("|".join(str(v) for v in row).encode()).hexdigest()[:TAG_HEX]


async def pickup_tag(maker: async_sessionmaker[AsyncSession], principal: PrincipalInfo) -> str:
    """The pickup's `X-Jpanel-Post`, or "" for a panel the gate refuses. Never raises: the pickup
    is the panel's proof of life and the debug relay, and a post tag is not worth either."""
    try:
        if not await agent_allowed(maker, principal):
            return ""
        owner_id = await owner_principal_id(maker)
        if owner_id is None:
            return ""
        return await post_tag(maker, SessionContext(principal_id=owner_id, principal_kind="owner"))
    except Exception as exc:  # noqa: BLE001 — the pickup answers whatever this does
        log.warning("panel_messages.tag_failed", error=repr(exc))
        return ""


async def _twin(request: Request, device_id: str) -> str:
    """The twin's panel name, or 404: only a live `jpet` panel is somebody to talk to."""
    names = await jpanel._panel_names(_maker(request))
    if device_id not in names:
        raise HTTPException(status_code=404, detail="no such panel")
    return names[device_id]


@router.get("/{device_id}")
async def thread(
    device_id: str,
    principal: PanelDep,
    request: Request,
    limit: int = Query(THREAD_DEFAULT, ge=1, le=THREAD_MAX),
) -> PostThread:
    """The owner's conversation with this twin: its last `limit` messages, oldest first."""
    owner = await owner_for_panel(request, principal)
    name = await _twin(request, device_id)
    rows = await jpanel.thread_with(_maker(request), owner, device_id, limit)
    return PostThread(
        device_id=device_id,
        name=name,
        tag=await post_tag(_maker(request), owner),
        messages=[_line(m) for m in rows],
    )


#: A page of older (or newer) messages: the panel's Load earlier asks ten at a time
#: (`panel_convo.PAGE_MAX`) and keeps at most twenty a thread. Never more JSON than the panel's
#: page buffer holds: a page of long, much-escaped transcripts is cut short and the rest counted.
PAGE_MAX = 10
PAGE_BYTES = 4000


class PostPage(BaseModel):
    """A page oldest first, how many remain before its first and after its last (or either side
    of the anchor when none came back), and — on the page that reaches it — when the
    conversation with her began."""

    device_id: str
    messages: list[PostLine]
    older: int
    newer: int
    start_at: int | None


def _line(m: jpanel.Message) -> PostLine:
    return PostLine(
        id=m.id,
        mine=m.direction == "out",
        text=screen_text(" ".join(m.transcript.split()), TEXT_BYTES)[0],
        ms=m.duration_ms,
        played=m.played_at is not None,
        voice=m.composed == "voice",
    )


def _fit(lines: list[PostLine], newest_first: bool) -> list[PostLine]:
    """As many of `lines` (oldest first) as fit PAGE_BYTES of JSON, kept from the anchor's side."""
    order = list(reversed(lines)) if newest_first else list(lines)
    kept: list[PostLine] = []
    used = 128
    for line in order:
        size = len(line.model_dump_json().encode()) + 1
        if kept and used + size > PAGE_BYTES:
            break
        kept.append(line)
        used += size
    return list(reversed(kept)) if newest_first else kept


@router.get("/{device_id}/history")
async def history(
    device_id: str,
    principal: PanelDep,
    request: Request,
    before: str | None = None,
    after: str | None = None,
    limit: int = Query(PAGE_MAX, ge=0, le=PAGE_MAX),
) -> PostPage:
    """Load earlier in her thread (firmware-c6 0.0.21): the `limit` messages just before `before`
    (the newest, with neither) or just after `after`, oldest first, with the counts either side.
    `limit=0` is the counts alone. 404 for an anchor that is not a message of this conversation."""
    if before and after:
        raise HTTPException(status_code=422, detail="before or after, not both")
    owner = await owner_for_panel(request, principal)
    await _twin(request, device_id)
    page = await jpanel.thread_page(
        _maker(request), owner, device_id, before=before or None, after=after or None, limit=limit
    )
    if page is None:
        raise HTTPException(status_code=404, detail="no such message")
    lines = _fit([_line(m) for m in page.messages], newest_first=not after)
    older, newer = page.older, page.newer
    # What the byte cap left out is still there: counted on the side it was cut from.
    if lines and len(lines) < len(page.messages):
        if after:
            newer += len(page.messages) - len(lines)
        else:
            older += len(page.messages) - len(lines)
    return PostPage(
        device_id=device_id,
        messages=lines,
        older=older,
        newer=newer,
        start_at=int(page.start_at.timestamp()) if page.start_at and older == 0 else None,
    )


@router.get("/{device_id}/{message_id}/pcm")
async def pcm(device_id: str, message_id: str, principal: PanelDep, request: Request) -> Response:
    """One message's audio as 16 kHz mono s16, with `X-Jpanel-Sha256` — the body a twin's panel
    replays (`jpanel.pcm_reply`). Streamed by the panel into its speaker, never held whole."""
    owner = await owner_for_panel(request, principal)
    found = await jpanel.owner_row(_maker(request), owner, device_id, message_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such message")
    return await jpanel.pcm_reply(request, found[0], message_id)


@router.post("/{device_id}/{message_id}/played", status_code=204)
async def played(
    device_id: str, message_id: str, principal: PanelDep, request: Request
) -> Response:
    """The owner heard hers on the desk panel: the PWA's stamp (`jpanel.mark_owner_played`), so it
    shows played there too. Only a message she sent him — his own are hers to play."""
    owner = await owner_for_panel(request, principal)
    found = await jpanel.owner_row(_maker(request), owner, device_id, message_id)
    if found is None or not found[1]:
        raise HTTPException(status_code=404, detail="no such message")
    await jpanel.mark_owner_played(request, owner, message_id)
    return Response(status_code=204)


def _verified(body: bytes, claimed: str, device: str) -> bytes:
    """The audio, once it is known to be what the panel recorded: a trailing digest or a header
    one, compared as `jpanel.send` compares it. A mismatch is a 422 — the panel says it was not
    sent rather than file a fragment of Dad's sentence."""
    if claimed == TRAILER:
        if len(body) < DIGEST_BYTES:
            raise HTTPException(status_code=422, detail="audio did not survive the upload")
        audio, digest = body[:-DIGEST_BYTES], body[-DIGEST_BYTES:]
        ok = hashlib.sha256(audio).digest() == digest
    elif claimed:
        audio = body
        ok = hashlib.sha256(audio).hexdigest() == claimed
    else:
        log.info("panel_messages.upload_unverified", device=device, bytes=len(body))
        return body
    if not ok:
        log.warning("panel_messages.upload_corrupt", device=device, bytes=len(body))
        raise HTTPException(status_code=422, detail="audio did not survive the upload")
    return audio


@router.post("/{device_id}/send", status_code=201)
async def send(device_id: str, principal: PanelDep, request: Request) -> Sent:
    """Dad's voice from the desk panel to this twin: raw 16 kHz mono s16, chunked as it was
    recorded, filed through the PWA's own path (`jpanel.file_owner_voice`) so her panel receives
    it exactly as if he had spoken into his phone."""
    owner = await owner_for_panel(request, principal)
    await _twin(request, device_id)
    claimed = request.headers.get("X-Jpanel-Sha256", "").strip().lower()
    audio = _verified(await request.body(), claimed, device_id)
    if not audio:
        raise HTTPException(status_code=400, detail="no audio")
    if len(audio) > jpanel.MAX_MESSAGE_BYTES:
        raise HTTPException(
            status_code=413, detail=f"message longer than {jpanel.MAX_MESSAGE_MS // 1000}s"
        )
    msg = await jpanel.file_owner_voice(request, owner, audio, device_id)
    log.info("panel_messages.sent", to=device_id, ms=msg.duration_ms)
    return Sent(
        id=msg.id,
        text=screen_text(" ".join(msg.transcript.split()), TEXT_BYTES)[0],
        ms=msg.duration_ms,
    )
