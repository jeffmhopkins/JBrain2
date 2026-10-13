"""The desk panel's conversation switcher: Recent, switch, and a switched thread's last words.

docs/plans/JPANEL_C6_PLAN.md, owner decision 2026-10-12 (firmware-c6 0.0.15): *"On long press of
the Jerv [conversation] … another button so that you can click it and view recent conversations
to re-change what conversation we're actually in."* The panel lists one agent's last ten chats —
the ones started at the panel and the ones started in the PWA (`agent_sessions.origin`,
migration 0239) — and pointing it at one makes the next talk continue THAT chat, through the
same `endpoint_panel_thread` row `panel_agent.panel_thread` already reads.

THE SAME GATE AS THE TALK. Every route here answers only a caller `panel_agent.agent_allowed`
lets reach the agents — a `display` panel the owner switched on — so a twin's panel, a revoked
key or a display the owner has not allowed gets 403 and learns nothing, not even whether the
owner has chats. What it reads and writes is the OWNER's (the live owner principal the PWA
uses, `auth.owner`), on owner-scoped sessions (#3): the panel names an agent and a session id,
and a session that is not the owner's root chat with that agent, from the panel or the PWA,
is 404 whatever id is sent.

"New conversation" has no route: the panel already asks for one with the talk's
`new_thread=true` (`/endpoint/converse`), so nothing is created until something is said.

Since firmware-c6 0.0.21 (owner, approved 2026-10-13) two more, behind the same gate and the
same scoping (`_owned`: never a domain- or subject-scoped chat):

- `GET …/history?before=|after=&limit=` pages the panel's current conversation ten messages at
  a time, oldest first, with how many remain either side and when it began. A message's id is
  its turn's `seq`, the same id the talk's stream gives the panel for the exchange it just had
  (`panel_stream.IDS`), so "Load earlier" pages from whatever is on the glass.
- `GET …/speech/{id}` speaks one stored answer, whole — not the 255 bytes the bubble shows — as
  the JT2 records the panel already plays a reply from (`panel_stream.listen`): Listen.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api import panel_agent, panel_stream
from jbrain.api.deps import PanelDep
from jbrain.api.endpoint import panel_speech
from jbrain.api.panel_agent import (
    AGENT_IDS,
    LEGACY_VOICE_PREFIX,
    PanelAgent,
    agent_allowed,
    condensed,
    screen_text,
    speakable,
)
from jbrain.auth.owner import owner_principal_id
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint/convo", tags=["endpoint"])

#: The panel's Recent list: ten rows (firmware `CONVO_ROWS`), each title and last line cut to
#: what a row keeps (`CONVO_TITLE` 40, `CONVO_SNIPPET` 80 bytes, terminator apart).
RECENT_MAX = 10
TITLE_BYTES = 40
SNIPPET_BYTES = 80
#: A switched thread: at most this many exchanges (the panel keeps eight messages). The newest
#: answer up to the panel's long text (`MSG_LONG_TEXT`, `panel_stream.TEXT_BYTES`), every other
#: message a bubble's 255 bytes.
PAIRS_MAX = 4
NEWEST_BYTES = 1000
BUBBLE_BYTES = 255
YOU = "You: "
#: What a chat with no name is called: the panel's own placeholder, or the PWA's.
PANEL_UNTITLED = "Desk panel"
PWA_UNTITLED = "Untitled chat"


class RecentItem(BaseModel):
    id: str
    title: str
    #: Unix seconds; the panel says "12 min ago" / "Yesterday 19:40" from its own clock and zone.
    at: int
    origin: str  # "panel" | "pwa"
    snippet: str
    current: bool


class Recent(BaseModel):
    items: list[RecentItem]


class Select(BaseModel):
    session_id: str


class Selected(BaseModel):
    id: str
    title: str


class Line(BaseModel):
    mine: bool
    text: str
    #: The turn's `seq` (0.0.21): what Listen and Load earlier name this message by.
    id: int = 0


class Thread(BaseModel):
    id: str
    title: str
    messages: list[Line]
    #: Older messages exist than these: the panel's "Earlier messages are in the app".
    more: bool


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


async def owner_for_panel(request: Request, principal: PrincipalInfo) -> SessionContext:
    """The gate, then the owner the panel acts for. 403 for any caller the talk would refuse."""
    maker = _maker(request)
    if not await agent_allowed(maker, principal):
        log.warning("panel_convo.refused", device=principal.id)
        raise HTTPException(status_code=403, detail="this panel cannot reach the agents")
    owner_id = await owner_principal_id(maker)
    if owner_id is None:
        raise HTTPException(status_code=503, detail="no owner on this box")
    return SessionContext(principal_id=owner_id, principal_kind="owner")


def title_of(title: str, origin: str | None) -> str:
    """A chat's name as the panel shows it, cut to a row's bytes."""
    shown = " ".join(title.split()) or (PANEL_UNTITLED if origin == "panel" else PWA_UNTITLED)
    return screen_text(shown, TITLE_BYTES)[0]


def _plain(role: str, content: str) -> str:
    said = content.removeprefix(LEGACY_VOICE_PREFIX) if role == "user" else content
    return speakable(said)


def snippet_of(role: str, content: str) -> str:
    """A row's last line: "You: " before the owner's own, cut on a word to SNIPPET_BYTES."""
    prose = _plain(role, content)
    if not prose:
        return ""
    return screen_text((YOU if role == "user" else "") + prose, SNIPPET_BYTES)[0]


async def _current(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, subject: str, agent_id: str
) -> str | None:
    async with scoped_session(maker, owner) as s:
        found = (
            await s.execute(
                text(
                    "SELECT agent_session_id::text FROM app.endpoint_panel_thread"
                    " WHERE subject_id = CAST(:sid AS uuid) AND agent = :agent"
                ),
                {"sid": subject, "agent": agent_id},
            )
        ).scalar_one_or_none()
    return None if found is None else str(found)


@router.get("/{agent}/recent")
async def recent(agent: PanelAgent, principal: PanelDep, request: Request) -> Recent:
    """The owner's last ten chats with `agent` that were started at the panel or in the PWA,
    newest first with the panel's current one at the top: no sub-agent child, no in-game chat,
    no task run, and no chat with nothing said in it yet."""
    owner = await owner_for_panel(request, principal)
    maker = _maker(request)
    agent_id = AGENT_IDS[agent]
    current = await _current(maker, owner, principal.subject_id, agent_id)
    async with scoped_session(maker, owner) as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT s.id::text AS id, s.title, s.origin, s.last_active_at,
                           t.role, left(t.content, 2000) AS content
                    FROM app.agent_sessions s
                    JOIN LATERAL (
                        SELECT role, content FROM app.agent_turns
                        WHERE session_id = s.id ORDER BY seq DESC LIMIT 1
                    ) t ON true
                    WHERE s.principal_id = CAST(:pid AS uuid) AND s.agent = :agent
                      AND s.parent_session_id IS NULL AND s.origin IN ('panel', 'pwa')
                      AND COALESCE(cardinality(s.domain_scopes), 0) = 0
                      AND COALESCE(cardinality(s.subject_ids), 0) = 0
                      AND (s.status <> 'archived' OR s.id = CAST(:cur AS uuid))
                    ORDER BY COALESCE(s.id = CAST(:cur AS uuid), false) DESC,
                             s.last_active_at DESC, s.id
                    LIMIT :n
                    """
                ),
                {"pid": owner.principal_id, "agent": agent_id, "cur": current, "n": RECENT_MAX},
            )
        ).all()
    return Recent(
        items=[
            RecentItem(
                id=r.id,
                title=title_of(r.title, r.origin),
                at=int(r.last_active_at.timestamp()),
                origin=r.origin,
                snippet=snippet_of(r.role, r.content),
                current=r.id == current,
            )
            for r in rows
        ]
    )


async def _owned(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, agent_id: str, sid: str
) -> Any:
    """The owner's root chat `sid` with this agent, from the panel or the PWA — or None, for an
    id the Recent list would not show (another agent's, a child, an in-game chat, archived,
    empty, not a uuid). A chat opened with domain or subject scopes is never the panel's: its
    turns read health, finance or location data and the panel speaks its answers aloud."""
    try:
        uuid.UUID(sid)
    except ValueError:
        return None
    async with scoped_session(maker, owner) as s:
        return (
            await s.execute(
                text(
                    """
                    SELECT s.id::text AS id, s.title, s.origin FROM app.agent_sessions s
                    WHERE s.id = CAST(:sid AS uuid) AND s.principal_id = CAST(:pid AS uuid)
                      AND s.agent = :agent AND s.parent_session_id IS NULL
                      AND s.origin IN ('panel', 'pwa') AND s.status <> 'archived'
                      AND COALESCE(cardinality(s.domain_scopes), 0) = 0
                      AND COALESCE(cardinality(s.subject_ids), 0) = 0
                      AND EXISTS (SELECT 1 FROM app.agent_turns t WHERE t.session_id = s.id)
                    """
                ),
                {"sid": sid, "pid": owner.principal_id, "agent": agent_id},
            )
        ).first()


@router.post("/{agent}/select")
async def select(
    agent: PanelAgent, body: Select, principal: PanelDep, request: Request
) -> Selected:
    """Point this panel's thread with `agent` at one of the owner's chats: the next talk
    continues it (`panel_agent.panel_thread`). 404 for a chat the Recent list would not show."""
    owner = await owner_for_panel(request, principal)
    maker = _maker(request)
    agent_id = AGENT_IDS[agent]
    row = await _owned(maker, owner, agent_id, body.session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.endpoint_panel_thread (subject_id, agent, agent_session_id)
                VALUES (CAST(:subject AS uuid), :agent, CAST(:sid AS uuid))
                ON CONFLICT (subject_id, agent) DO UPDATE
                SET agent_session_id = EXCLUDED.agent_session_id, updated_at = now()
                """
            ),
            {"subject": principal.subject_id, "agent": agent_id, "sid": row.id},
        )
        await s.commit()
    log.info("panel_convo.selected", agent=agent_id, session=row.id, device=principal.id)
    return Selected(id=row.id, title=title_of(row.title, row.origin))


def lines(turns: list[tuple[str, str]], ids: list[int] | None = None) -> list[Line]:
    """`turns` (oldest first) as the panel's bubbles: each its plain prose, the newest answer
    up to NEWEST_BYTES and every other message BUBBLE_BYTES — an answer in whole sentences with
    " More in the app." when cut (`panel_agent.condensed`), the owner's words cut on a word.
    `ids` (the turns' `seq`, in the same order) name each bubble for Listen and Load earlier."""
    newest = max((i for i, (role, _) in enumerate(turns) if role == "assistant"), default=-1)
    out: list[Line] = []
    for i, (role, content) in enumerate(turns):
        prose = _plain(role, content)
        if not prose:
            continue
        mid = ids[i] if ids else 0
        if role == "user":
            out.append(Line(mine=True, text=screen_text(prose, BUBBLE_BYTES)[0], id=mid))
        else:
            limit = NEWEST_BYTES if i == newest else BUBBLE_BYTES
            out.append(Line(mine=False, text=condensed(prose, limit=limit), id=mid))
    return out


@router.get("/{agent}/messages")
async def messages(
    agent: PanelAgent,
    principal: PanelDep,
    request: Request,
    limit: int = Query(3, ge=0, le=PAIRS_MAX),
) -> Thread:
    """The panel's current thread with `agent`: its title, and its last `limit` exchanges for
    the bubbles (0: the title alone, which the panel asks for when the thread opens). An empty
    id and title when the panel has no thread with this agent yet."""
    owner = await owner_for_panel(request, principal)
    maker = _maker(request)
    agent_id = AGENT_IDS[agent]
    current = await _current(maker, owner, principal.subject_id, agent_id)
    row = await _owned(maker, owner, agent_id, current) if current else None
    if row is None:
        return Thread(id="", title="", messages=[], more=False)
    want = 2 * limit
    async with scoped_session(maker, owner) as s:
        got = (
            await s.execute(
                text(
                    "SELECT seq, role, content FROM app.agent_turns"
                    " WHERE session_id = CAST(:sid AS uuid) ORDER BY seq DESC LIMIT :n"
                ),
                {"sid": row.id, "n": want + 1},
            )
        ).all()
    more = len(got) > want
    kept = list(reversed(got[:want]))
    return Thread(
        id=row.id,
        title=title_of(row.title, row.origin),
        messages=lines([(r.role, r.content) for r in kept], [int(r.seq) for r in kept]),
        more=more,
    )


# ---- 0.0.21: Load earlier, and Listen -----------------------------------------------------------

#: A page of history: ten messages (the mock's "fetches 10 older"), and never more JSON than the
#: panel's page buffer holds (firmware `HIST_BODY_MAX`): a page of long, much-escaped bubbles is
#: cut short and the rest stay counted in `older` / `newer`.
PAGE_MAX = 10
PAGE_BYTES = 4000


class PageLine(BaseModel):
    id: int
    mine: bool
    text: str


class Page(BaseModel):
    """Messages oldest first; how many remain before the first and after the last (or either side
    of the anchor, when none came back); and, at the true start, when the conversation began."""

    messages: list[PageLine]
    older: int
    newer: int
    start_at: int | None


#: A turn the panel can show: one with words in it.
_SHOWN = "session_id = CAST(:sid AS uuid) AND btrim(content) <> ''"


def page_line(seq: int, role: str, content: str) -> PageLine:
    """One message of an older page: its plain prose cut to a bubble on a word — Listen reads
    the rest — or "…" for a turn whose words were all markup."""
    text_ = screen_text(_plain(role, content), BUBBLE_BYTES)[0] or "…"
    return PageLine(id=seq, mine=role == "user", text=text_)


def fit_page(lines_: list[PageLine], newest_first: bool) -> list[PageLine]:
    """As many of `lines_` (oldest first) as fit PAGE_BYTES of JSON, kept from the anchor's side:
    the newest when paging back (`newest_first`), the oldest when paging forward. Never none."""
    order = list(reversed(lines_)) if newest_first else list(lines_)
    kept: list[PageLine] = []
    used = 64
    for line in order:
        size = len(line.model_dump_json().encode()) + 1
        if kept and used + size > PAGE_BYTES:
            break
        kept.append(line)
        used += size
    return list(reversed(kept)) if newest_first else kept


async def _count(s: AsyncSession, sid: str, where: str, **params: Any) -> int:
    got = await s.execute(
        text(f"SELECT count(*) FROM app.agent_turns WHERE {_SHOWN} AND {where}"),  # noqa: S608
        {"sid": sid, **params},
    )
    return int(got.scalar_one())


@router.get("/{agent}/history")
async def history(
    agent: PanelAgent,
    principal: PanelDep,
    request: Request,
    before: int | None = Query(None, ge=1),
    after: int | None = Query(None, ge=1),
    limit: int = Query(PAGE_MAX, ge=0, le=PAGE_MAX),
) -> Page:
    """A page of the panel's current conversation with `agent`: the `limit` messages just before
    `before` (the newest, with neither), or just after `after`. `limit=0` is the counts alone,
    which the panel asks for when a thread opens, for "Load earlier · N more"."""
    if before is not None and after is not None:
        raise HTTPException(status_code=422, detail="before or after, not both")
    owner = await owner_for_panel(request, principal)
    maker = _maker(request)
    agent_id = AGENT_IDS[agent]
    current = await _current(maker, owner, principal.subject_id, agent_id)
    row = await _owned(maker, owner, agent_id, current) if current else None
    if row is None:
        return Page(messages=[], older=0, newer=0, start_at=None)
    sid = str(row.id)
    async with scoped_session(maker, owner) as s:
        if after is not None:
            sql = (
                f"SELECT seq, role, content FROM app.agent_turns WHERE {_SHOWN}"  # noqa: S608
                " AND seq > :p ORDER BY seq LIMIT :n"
            )
            got = (await s.execute(text(sql), {"sid": sid, "p": after, "n": limit})).all()
            page = fit_page([page_line(int(r.seq), r.role, r.content) for r in got], False)
        else:
            cond = "seq < :p" if before is not None else "seq > 0"
            sql = (
                f"SELECT seq, role, content FROM app.agent_turns WHERE {_SHOWN}"  # noqa: S608
                f" AND {cond} ORDER BY seq DESC LIMIT :n"
            )
            got = (await s.execute(text(sql), {"sid": sid, "p": before or 0, "n": limit})).all()
            rows = [page_line(int(r.seq), r.role, r.content) for r in reversed(got)]
            page = fit_page(rows, True)
        if page:
            older = await _count(s, sid, "seq < :p", p=page[0].id)
            newer = await _count(s, sid, "seq > :p", p=page[-1].id)
        elif after is not None:
            older = await _count(s, sid, "seq <= :p", p=after)
            newer = await _count(s, sid, "seq > :p", p=after)
        elif before is not None:
            older = await _count(s, sid, "seq < :p", p=before)
            newer = await _count(s, sid, "seq > :p", p=before)
        else:
            older, newer = await _count(s, sid, "seq > 0"), 0
        start_at = None
        if older == 0:
            first = (
                await s.execute(
                    text(f"SELECT min(created_at) FROM app.agent_turns WHERE {_SHOWN}"),  # noqa: S608
                    {"sid": sid},
                )
            ).scalar_one()
            start_at = int(first.timestamp()) if first is not None else None
    return Page(messages=page, older=older, newer=newer, start_at=start_at)


@router.get("/{agent}/speech/{turn_id}")
async def speech(
    agent: PanelAgent, turn_id: int, principal: PanelDep, request: Request
) -> StreamingResponse:
    """One of the agent's stored answers spoken whole, in the PWA read-aloud's voice, as JT2:
    SPOKEN marks and PCM sentence by sentence, then END (`panel_stream.listen`). Only an answer
    in one of the owner's chats the panel may show (`_owned`); 404 for anything else, a question
    included. 503 `tts_unavailable` when the box cannot speak."""
    owner = await owner_for_panel(request, principal)
    maker = _maker(request)
    agent_id = AGENT_IDS[agent]
    async with scoped_session(maker, owner) as s:
        turn = (
            await s.execute(
                text(
                    "SELECT session_id::text AS sid, role, content FROM app.agent_turns"
                    " WHERE seq = :seq"
                ),
                {"seq": turn_id},
            )
        ).first()
    if turn is None or turn.role != "assistant":
        raise HTTPException(status_code=404, detail="no such answer")
    if await _owned(maker, owner, agent_id, str(turn.sid)) is None:
        raise HTTPException(status_code=404, detail="no such answer")
    words = _plain("assistant", turn.content)
    if not words:
        raise HTTPException(status_code=404, detail="no such answer")
    settings = cast(Settings, request.app.state.settings)
    if not (settings.brain_tts_url or "").strip():
        raise HTTPException(status_code=503, detail={"reason": "tts_unavailable"})
    store = cast(SqlSettingsStore, request.app.state.settings_store)
    voice: panel_agent.Voice | None
    try:
        voice = await panel_agent.owner_voice(store, owner)
    except Exception as exc:  # noqa: BLE001 — the default voice rather than none
        log.warning("panel_convo.listen_voice_error", error=repr(exc))
        voice = None

    async def say(chunk: str, budget: int) -> bytes:
        return await panel_speech(settings, chunk, budget, voice)

    stats: dict[str, Any] = {}

    async def body() -> AsyncIterator[bytes]:
        try:
            async for rec in panel_stream.listen(words, say, stats):
                yield rec
        finally:
            log.info("panel_convo.listen", agent=agent_id, turn=turn_id, **stats)

    return StreamingResponse(
        body(),
        media_type="application/x-jpanel-turn",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
