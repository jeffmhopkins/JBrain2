"""The owner's desk panel talking to the REAL agents: Jerv, and Minecraft_Dave.

docs/plans/JPANEL_C6_PLAN.md, owner decision 2026-10-12: *"it should be the full jerv with full
tool access. I'm explicitly permitting this."* — and, the same day, Dave behind a Minecraft
submenu on the same panel. A spoken turn from that panel runs through the PWA's own `/chat`
handler (`api.agent.chat`), as the owner, into one rolling "Desk panel" thread per agent: the
same prompt, tools, memory, approvals and transcript as a turn typed in the PWA, which is also
where the exchange is read afterwards.

THE GATE IS THE WHOLE SECURITY STORY. A device key that reaches this is worth the owner's
agent, so `agent_allowed` asks Postgres, under the PANEL's own RLS context, two things no
request can change: that the caller's subject was flashed as a `display` (the twins' panels are
`jpet`), and that the owner switched `endpoint_panel.agent_access` on for it. A panel reads its
own row and writes none (migration 0233), so nothing the firmware sends can open it.

NOTHING IS APPROVED HERE. A Proposal the agent stages waits for the owner's tap in the PWA; an
`ask_owner` question waits for his answer there. The panel only says so out loud.

Rules: the LLM is reached only through the agent loop the PWA uses (#1); every query runs on an
RLS-scoped session (#3); the TTS and STT are the on-box services the panels already use.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.agents import MINECRAFT_DAVE_AGENT
from jbrain.agent.session import PANEL_PLACEHOLDER_TITLE, AgentSessionRepo
from jbrain.api.brain import apply_pronunciation
from jbrain.api.deps import DebugDep, OwnerDep, PanelDep
from jbrain.api.notes import ctx_for
from jbrain.auth.owner import owner_principal_id
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, panel_context, scoped_session
from jbrain.models.note_conversation import AWAITING_OWNER
from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint", tags=["endpoint"])

#: What the panel names on the wire (`persona=`), and the agent each one is.
PanelAgent = Literal["jerv", "dave"]
AGENT_IDS: dict[PanelAgent, str] = {"jerv": "jerv", "dave": MINECRAFT_DAVE_AGENT}
SPOKEN_NAMES: dict[PanelAgent, str] = {"jerv": "Jerv", "dave": "Dave"}
#: A placeholder, not a name: Jerv titles the chat in its first turn (`session.is_unnamed`).
THREAD_TITLE = PANEL_PLACEHOLDER_TITLE

#: How long the panel's request waits for the turn. The firmware waits 130 s for the box's
#: answer (`ANSWER_LIMIT_MS`, 0.0.11); transcription and speech come out of the rest. A turn
#: still running at this point is NOT cancelled: it finishes and lands in the thread, and the
#: panel is told where to read it.
AGENT_WAIT_S = 100.0

#: The panel keeps 255 bytes of a message (`msg_t.text`); the reply is condensed to fit rather
#: than cut mid-word by the firmware, and the same words are spoken, so what is heard is what
#: is shown. The full answer is in the thread.
REPLY_BYTES = 240

# What 0.0.11-0.0.13 put in front of the owner's words to ask for a short, speakable answer.
# It was recorded with them, so the PWA showed it as his message (owner, 2026-10-12). A turn now
# says it is spoken with `ChatRequest.spoken` (agent.SPOKEN_TURN_BLOCK) and stores only what he
# said; the PWA strips this from the rows written before (`frontend/src/agent/fromTurn`).
LEGACY_VOICE_PREFIX = "(By voice from my desk panel: answer in a sentence or two, plain words.) "

STILL_WORKING = "Still working on that. The answer will be in the app."
#: JT2's cap on following a turn with progress flowing. Past it the panel is told the turn is
#: still going (it finishes into the "Desk panel" chat regardless); the deepest research runs
#: hours, and five minutes is long past anything a person stands at a panel for.
AGENT_STREAM_CAP_S = 300.0
STILL_WORKING_STREAM = (
    "This is taking a while, so I'll keep at it. The answer will be in our Desk panel chat."
)
BUSY = "I'm still on your last question. Its answer will be in the app."
#: JT2: the panel is still connected to that turn's answer only if it is the one waiting.
BUSY_STREAM = "I'm still on your last question. Ask me again once it's answered."
FAILED = "Sorry, I couldn't finish that just now. Try me again?"
ASKED = " I've put a question for you in the app."
STAGED = " It's waiting for your OK in the app."
MORE = " More in the app."


# ---- the gate ------------------------------------------------------------------------------


async def agent_allowed(maker: async_sessionmaker[AsyncSession], principal: PrincipalInfo) -> bool:
    """Whether this caller is a panel the owner let reach the agents.

    Read under the panel's OWN context: `principals_select`, `subjects_access` and
    `endpoint_panel_own` each open exactly the caller's own row, so the answer comes from rows
    the server wrote — the flash's role and the owner's switch — never from the request. Fails
    closed: an owner cookie, a revoked key, a `jpet`, a missing row or a database error is
    False."""
    if principal.kind != "device_key" or not principal.subject_id:
        return False
    try:
        async with scoped_session(maker, panel_context(principal.id, principal.subject_id)) as s:
            row = (
                await s.execute(
                    text(
                        """
                        SELECT sub.device_role = 'display' AND ep.agent_access
                        FROM app.principals p
                        JOIN app.subjects sub ON sub.id = p.subject_id
                        JOIN app.endpoint_panel ep ON ep.subject_id = sub.id
                        WHERE p.id = CAST(:pid AS uuid) AND p.kind = 'device_key'
                          AND p.revoked_at IS NULL
                        """
                    ),
                    {"pid": principal.id},
                )
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — a gate that cannot answer is closed
        log.warning("panel_agent.gate_unreadable", error=repr(exc))
        return False
    return row is True


# ---- the thread ----------------------------------------------------------------------------


async def panel_thread(
    maker: async_sessionmaker[AsyncSession],
    sessions: AgentSessionRepo,
    owner: SessionContext,
    subject_id: str,
    agent: PanelAgent,
    *,
    fresh: bool,
) -> str:
    """The agent session this panel's turns land in: one rolling thread per agent, in the
    owner's history as "Desk panel". `fresh` (the panel's Clear chat) starts a new one; the old
    thread stays in the PWA. A thread the owner deleted cascades away and is replaced here."""
    agent_id = AGENT_IDS[agent]
    if not fresh:
        async with scoped_session(maker, owner) as s:
            found = (
                await s.execute(
                    text(
                        "SELECT agent_session_id::text FROM app.endpoint_panel_thread"
                        " WHERE subject_id = CAST(:sid AS uuid) AND agent = :agent"
                    ),
                    {"sid": subject_id, "agent": agent_id},
                )
            ).scalar_one_or_none()
        if found is not None:
            await _rehome(maker, owner, str(found))
            return str(found)
    made = await sessions.create(
        owner, domain_scopes=[], title=THREAD_TITLE, agent=agent_id, origin="panel"
    )
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.endpoint_panel_thread (subject_id, agent, agent_session_id)
                VALUES (CAST(:sid AS uuid), :agent, CAST(:session AS uuid))
                ON CONFLICT (subject_id, agent) DO UPDATE
                SET agent_session_id = EXCLUDED.agent_session_id, updated_at = now()
                """
            ),
            {"sid": subject_id, "agent": agent_id, "session": str(made.id)},
        )
        await s.commit()
    log.info("panel_agent.thread_started", agent=agent_id, session=str(made.id), fresh=fresh)
    return str(made.id)


async def _rehome(maker: async_sessionmaker[AsyncSession], owner: SessionContext, sid: str) -> None:
    """File a panel thread under the owner principal his PWA uses, if it is not already.

    0.0.11's turns resolved the owner as `WHERE kind='owner' LIMIT 1` and so, on a box whose
    owner key had been rotated, filed the "Desk panel" thread under the old, revoked principal
    (2026-10-12). Migration 0234 moved the threads that existed then; this keeps any thread
    where it belongs on every turn after a later rotation too. Its Proposals move with it, so a
    change Jerv staged from the panel is reviewed as the owner who will approve it. One UPDATE
    that matches nothing in the normal case."""
    async with scoped_session(maker, owner) as s:
        moved = len(
            (
                await s.execute(
                    text(
                        """
                        UPDATE app.agent_sessions SET principal_id = CAST(:pid AS uuid)
                        WHERE (id = CAST(:sid AS uuid) OR parent_session_id = CAST(:sid AS uuid))
                          AND principal_id <> CAST(:pid AS uuid)
                        RETURNING id
                        """
                    ),
                    {"pid": owner.principal_id, "sid": sid},
                )
            ).all()
        )
        if moved:
            await s.execute(
                text(
                    """
                    UPDATE app.proposals SET principal_id = CAST(:pid AS uuid)
                    WHERE session_id IN (
                        SELECT id FROM app.agent_sessions
                        WHERE id = CAST(:sid AS uuid) OR parent_session_id = CAST(:sid AS uuid)
                    ) AND principal_id <> CAST(:pid AS uuid)
                    """
                ),
                {"pid": owner.principal_id, "sid": sid},
            )
            log.info("panel_agent.thread_rehomed", session=sid, sessions=moved)
        await s.commit()


# ---- the words -----------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_FOOTNOTE = re.compile(r"\[\^?\d+\]|\[=\d+\]")
_LINE_MARK = re.compile(r"^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?)", re.MULTILINE)
_INLINE = re.compile(r"[*_`~|]+")
_URL = re.compile(r"https?://\S+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def speakable(markdown: str) -> str:
    """An answer as plain prose: what the panel shows and what it says. Code blocks, links'
    targets, citation markers and Markdown marks go; a list's items become sentences."""
    s = _FENCE.sub(" ", markdown)
    s = _LINK.sub(r"\1", s)
    s = _FOOTNOTE.sub("", s)
    s = _URL.sub("", s)
    s = _LINE_MARK.sub("", s)
    s = _INLINE.sub("", s)
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    s = " ".join(ln if ln[-1] in ".!?:;," else f"{ln}." for ln in lines)
    return re.sub(r"\s+", " ", s).strip()


def condensed(prose: str, suffix: str = "", limit: int = REPLY_BYTES) -> str:
    """Whole sentences of `prose` that fit `limit` UTF-8 bytes with `suffix`; " More in the
    app." when any were left out. A first sentence too long on its own is cut on a word."""

    def size(t: str) -> int:
        return len(t.encode("utf-8"))

    sentences = [x for x in _SENTENCE.split(prose) if x]
    kept: list[str] = []
    for sentence in sentences:
        if size(" ".join([*kept, sentence]) + suffix + MORE) > limit:
            break
        kept.append(sentence)
    if len(kept) == len(sentences):
        # Each kept sentence fit with room for " More in the app." too, so all of them do.
        return (" ".join(kept) + suffix).strip()
    if not kept:
        room = limit - size("…" + suffix + MORE)
        words = prose.encode("utf-8")[: max(room, 0)].decode("utf-8", "ignore").rsplit(" ", 1)[0]
        return f"{words}…{suffix}{MORE}".strip()
    return (" ".join(kept) + suffix + MORE).strip()


# ---- the voice -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Voice:
    """The PWA read-aloud's own settings, so the panel speaks in the voice the owner picked."""

    params: dict[str, str]
    lexicon: dict[str, str]
    #: The PWA reads aloud with the phone's own voice when this is "native" — there is no box
    #: voice to match then, so the panel keeps the PWA's chosen Kokoro voice.
    native: bool = False


async def owner_voice(store: SqlSettingsStore, owner: SessionContext) -> Voice:
    """`brain_answer_voice` and its effects, as `/api/brain/tts` would receive them from the
    PWA's read-aloud (`useReadAloud`: voice, speed, pitch, chorus, robot), plus the owner's
    pronunciation lexicon that route applies."""
    params = {"voice": await store.brain_answer_voice(owner)}
    speed = await store.brain_answer_speed(owner)
    pitch = await store.brain_answer_pitch(owner)
    if speed != 1.0:
        params["speed"] = str(speed)
    if pitch:
        params["pitch"] = str(pitch)
    if await store.brain_answer_chorus(owner):
        params["chorus"] = "1"
    if await store.brain_answer_robot(owner):
        params["robot"] = "1"
    try:
        lexicon = await store.pronunciation_lexicon(owner)
    except Exception as exc:  # noqa: BLE001 — a respelling is a nicety, never a blocker
        log.warning("panel_agent.lexicon_unavailable", error=repr(exc))
        lexicon = {}
    native = await store.brain_read_aloud_engine(owner) == "native"
    return Voice(params=params, lexicon=lexicon, native=native)


def tts_params(voice: Voice | None, words: str) -> dict[str, str]:
    """The query the on-box `/tts` takes for `words` in `voice` (None: its default voice)."""
    if voice is None:
        return {"text": words}
    return {"text": apply_pronunciation(words, voice.lexicon), **voice.params}


# ---- the turn ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    words: str  # JT1: condensed to REPLY_BYTES, plain — shown and spoken
    stop_reason: str
    session_id: str
    waited_ms: int
    #: JT2: the whole answer as plain prose, with what waits for the owner said in words. The
    #: panel shows what fits (`screen_text`) and speaks all of it.
    full: str = ""


@dataclass
class Started:
    """A turn handed to the PWA's `/chat`: who it runs as, where it lands, and the live turn to
    follow — or, when there is nothing to follow (the thread is busy, the turn never started),
    the answer to give at once."""

    owner_ctx: SessionContext
    session_id: str
    t0: float
    live: Any = None
    early: Answer | None = None

    def answer(self, words: str, stop: str, full: str = "") -> Answer:
        waited = int((asyncio.get_running_loop().time() - self.t0) * 1000)
        return Answer(words, stop, self.session_id, waited, full or words)


async def start_turn(
    request: Request, principal: PrincipalInfo, agent: PanelAgent, heard: str, *, fresh: bool
) -> Started:
    """Start one spoken turn through the PWA's `/chat` handler, as the owner — the principal his
    PWA logins use (`auth.owner`) — in this panel's thread. Returns without waiting for it."""
    from jbrain.api import agent as agent_api  # the chat module imports half the app

    maker = cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)
    owner_id = await owner_principal_id(maker)
    if owner_id is None:
        raise HTTPException(status_code=503, detail="no owner on this box")
    owner = PrincipalInfo(id=owner_id, kind="owner", label="owner (desk panel)")
    owner_ctx = ctx_for(owner)
    sessions = agent_api.get_agent_sessions(request)
    session_id = await panel_thread(
        maker, sessions, owner_ctx, principal.subject_id, agent, fresh=fresh
    )
    started = Started(owner_ctx, session_id, asyncio.get_running_loop().time())
    try:
        resp = await agent_api.chat(
            request,
            owner,
            agent_api.ChatRequest(session_id=session_id, message=heard, spoken=True),
        )
    except HTTPException as exc:
        if exc.status_code in (409, 429):
            started.early = started.answer(BUSY, "busy", BUSY_STREAM)
            return started
        raise
    live = request.app.state.live_turns.get(resp.headers.get("X-Run-Id", ""))
    if live is None or live.task is None:
        started.early = started.answer(FAILED, "error")
    else:
        started.live = live
    return started


def settle(started: Started, agent: PanelAgent) -> Answer:
    """The answer of a turn that has finished (or failed): JT1's condensed words and JT2's whole
    prose, each with what waits for the owner."""
    acc = started.live.acc if started.live is not None else None
    stop = acc.stop_reason if acc is not None else "error"
    prose = speakable(acc.answer_text) if acc is not None else ""
    suffix = ""
    if stop == AWAITING_OWNER:
        suffix = ASKED
    elif acc is not None and any("proposal" in step for step in acc.tool_steps()):
        suffix = STAGED
    if not prose and not suffix:
        return started.answer(FAILED, stop)
    words = condensed(prose, suffix) if prose else suffix.strip()
    waits = waiting(acc, agent) if acc is not None else ""
    return started.answer(words, stop, " ".join(x for x in (prose, waits) if x))


async def run_turn(
    request: Request, principal: PrincipalInfo, agent: PanelAgent, heard: str, *, fresh: bool
) -> tuple[Answer, SessionContext]:
    """JT1's whole turn: started, waited on for `AGENT_WAIT_S`, settled. Never raises for the
    agent's own failure: the panel always has something to say."""
    started = await start_turn(request, principal, agent, heard, fresh=fresh)
    if started.early is not None:
        return started.early, started.owner_ctx
    try:
        # Shielded: the panel giving up waiting must never cancel the owner's turn.
        await asyncio.wait_for(asyncio.shield(started.live.task), timeout=AGENT_WAIT_S)
    except TimeoutError:
        return started.answer(STILL_WORKING, "still_running"), started.owner_ctx
    except Exception as exc:  # noqa: BLE001 — the turn's own failure is already recorded
        log.warning("panel_agent.turn_failed", error=repr(exc))
    return settle(started, agent), started.owner_ctx


# ---- what waits for the owner, in words (JT2) ---------------------------------------------

#: What a staged Proposal of each kind would do, for "Jerv wants to … — confirm it in the app".
_PROPOSAL_VERBS: dict[str, str] = {
    "appointment": "change your calendar",
    "merge": "merge two entries",
    "correction": "file a correction",
    "knowledge": "save a place",
    "owner-prefs": "save a standing instruction",
    "minecraft": "change the Minecraft server",
    "intake-link": "make an intake link",
    "remove-research-report": "remove a research report",
    "remove-library-video": "remove a saved video",
}
_QUESTION_CHARS = 140


def waiting(acc: Any, agent: PanelAgent) -> str:
    """What the turn left waiting for the owner, said plainly — the one case the panel still
    points at the app, because nothing is ever approved or answered from it. "" for nothing."""
    name = SPOKEN_NAMES[agent]
    steps = acc.tool_steps()
    if acc.stop_reason == AWAITING_OWNER:
        for step in steps:
            questions = (step.get("args") or {}).get("questions") or []
            first = questions[0] if questions and isinstance(questions[0], dict) else {}
            asked = str(first.get("question") or "").strip()
            if asked:
                if len(asked) > _QUESTION_CHARS:
                    asked = asked[:_QUESTION_CHARS].rsplit(" ", 1)[0] + "…"
                return f"{name} asks: {asked} Answer it in the app."
        return f"{name} has a question for you. Answer it in the app."
    for step in steps:
        proposal = step.get("proposal")
        if proposal:
            kind = str(proposal.get("kind") or "") if isinstance(proposal, dict) else ""
            verb = _PROPOSAL_VERBS.get(kind, "make a change")
            return f"{name} wants to {verb}. Confirm it in the app."
    return ""


def screen_text(full: str, limit: int) -> tuple[str, bool]:
    """`full` as the panel can keep it: whole when it fits `limit` UTF-8 bytes, else cut on a
    word with "…" (the speech still says all of it). Returns (text, was_cut)."""
    raw = full.encode("utf-8")
    if len(raw) <= limit:
        return full, False
    room = limit - len("…".encode())
    head = raw[: max(room, 0)].decode("utf-8", "ignore")
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "…", True


# ---- the owner's switch, and the panel's siblings --------------------------------------------


class AgentAccess(BaseModel):
    """Whether this panel's spoken turns reach the real Jerv and Dave."""

    agent_access: bool = False


async def _display_subject(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str
) -> str:
    """The subject of a live `display` panel key, or 404. Only a display can be given the
    agents: the twins' panels are refused here as well as at the gate."""
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None
    async with scoped_session(maker, ctx) as s:
        row = (
            await s.execute(
                text(
                    """
                    SELECT s.id::text, s.device_role FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.id = CAST(:dev AS uuid) AND p.kind = 'device_key'
                      AND s.device_role IS NOT NULL
                    """
                ),
                {"dev": device_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="no such panel")
    if row[1] != "display":
        raise HTTPException(
            status_code=409,
            detail="only a display panel can reach the agents — a pet panel never does",
        )
    return str(row[0])


async def read_access(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str
) -> AgentAccess:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        on = (
            await s.execute(
                text(
                    "SELECT agent_access FROM app.endpoint_panel"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": sid},
            )
        ).scalar_one_or_none()
    return AgentAccess(agent_access=on is True)


async def write_access(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str, on: bool
) -> AgentAccess:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.endpoint_panel (subject_id, agent_access)
                VALUES (CAST(:sid AS uuid), :on)
                ON CONFLICT (subject_id) DO UPDATE
                SET agent_access = EXCLUDED.agent_access, updated_at = now()
                """
            ),
            {"sid": sid, "on": on},
        )
        await s.commit()
    log.info("panel_agent.access_set", device=device_id, agent_access=on)
    return AgentAccess(agent_access=on)


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


@router.get("/panels/{device_id}/agent")
async def get_agent_access(device_id: str, owner: OwnerDep, request: Request) -> AgentAccess:
    """Whether this display panel's talks reach the real Jerv and Dave. Owner only."""
    return await read_access(_maker(request), ctx_for(owner), device_id)


@router.put("/panels/{device_id}/agent")
async def set_agent_access(
    device_id: str, owner: OwnerDep, request: Request, body: AgentAccess
) -> AgentAccess:
    """Let this display panel's spoken turns reach the real agents, as the owner — or stop
    them. Owner only; a pet panel is refused (409)."""
    return await write_access(_maker(request), ctx_for(owner), device_id, body.agent_access)


debug_router = APIRouter(prefix="/debug/panel", tags=["debug"])


@debug_router.put("/{device_id}/agent")
async def debug_set_agent_access(
    device_id: str, principal: DebugDep, request: Request, body: AgentAccess
) -> AgentAccess:
    """The same switch from the owner's debug console, which acts as the owner."""
    ctx = SessionContext(principal_id=principal.id, principal_kind="owner")
    log.info("debug.panel_agent_access", token=principal.label, device=device_id)
    return await write_access(_maker(request), ctx, device_id, body.agent_access)


@debug_router.get("/{device_id}/agent")
async def debug_get_agent_access(
    device_id: str, principal: DebugDep, request: Request
) -> AgentAccess:
    ctx = SessionContext(principal_id=principal.id, principal_kind="owner")
    return await read_access(_maker(request), ctx, device_id)


#: A panel shows at most this many siblings, each name at most this many UTF-8 bytes.
SIBLINGS_MAX = 4
SIBLING_NAME_BYTES = 24


class Sibling(BaseModel):
    device_id: str
    name: str


class Siblings(BaseModel):
    siblings: list[Sibling]


def _cut(name: str, limit: int = SIBLING_NAME_BYTES) -> str:
    return name.encode("utf-8")[:limit].decode("utf-8", "ignore").strip()


@router.get("/siblings")
async def siblings(principal: PanelDep, request: Request) -> Siblings:
    """The twins' panels by the names the owner gave them, for the desk panel's two message
    threads — so the panel shows the real names and binds each thread to a device id.

    Only a `display` panel (or the owner) is answered: a pet panel addresses "the other one"
    and the box decides who that is (`jpanel._panel_names`), so it has no use for the roster.
    Ids and names only — never a key, a label prefix or anything about the caller."""
    from jbrain.api.jpanel import _panel_names  # jpanel imports endpoint, which imports this

    maker = _maker(request)
    if principal.kind != "owner":
        sid = principal.subject_id
        role: Any = None
        if sid:
            async with scoped_session(maker, panel_context(principal.id, sid)) as s:
                role = (
                    await s.execute(
                        text("SELECT device_role FROM app.subjects WHERE id = CAST(:sid AS uuid)"),
                        {"sid": sid},
                    )
                ).scalar_one_or_none()
        if role != "display":
            raise HTTPException(status_code=403, detail="not a display panel")
    names = await _panel_names(maker)
    found = sorted(
        (
            Sibling(device_id=pid, name=_cut(name))
            for pid, name in names.items()
            if pid != principal.id
        ),
        key=lambda s: (s.name.casefold(), s.device_id),
    )
    return Siblings(siblings=found[:SIBLINGS_MAX])
