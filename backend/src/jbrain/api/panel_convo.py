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
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import PanelDep
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
from jbrain.db.session import SessionContext, scoped_session

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


class Thread(BaseModel):
    id: str
    title: str
    messages: list[Line]
    #: Older messages exist than these: the panel's "Earlier messages are in the app".
    more: bool


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


async def _owner(request: Request, principal: PrincipalInfo) -> SessionContext:
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
    owner = await _owner(request, principal)
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
    owner = await _owner(request, principal)
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


def lines(turns: list[tuple[str, str]]) -> list[Line]:
    """`turns` (oldest first) as the panel's bubbles: each its plain prose, the newest answer
    up to NEWEST_BYTES and every other message BUBBLE_BYTES — an answer in whole sentences with
    " More in the app." when cut (`panel_agent.condensed`), the owner's words cut on a word."""
    newest = max((i for i, (role, _) in enumerate(turns) if role == "assistant"), default=-1)
    out: list[Line] = []
    for i, (role, content) in enumerate(turns):
        prose = _plain(role, content)
        if not prose:
            continue
        if role == "user":
            out.append(Line(mine=True, text=screen_text(prose, BUBBLE_BYTES)[0]))
        else:
            limit = NEWEST_BYTES if i == newest else BUBBLE_BYTES
            out.append(Line(mine=False, text=condensed(prose, limit=limit)))
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
    owner = await _owner(request, principal)
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
                    "SELECT role, content FROM app.agent_turns"
                    " WHERE session_id = CAST(:sid AS uuid) ORDER BY seq DESC LIMIT :n"
                ),
                {"sid": row.id, "n": want + 1},
            )
        ).all()
    more = len(got) > want
    turns = [(r.role, r.content) for r in reversed(got[:want])]
    return Thread(
        id=row.id, title=title_of(row.title, row.origin), messages=lines(turns), more=more
    )
