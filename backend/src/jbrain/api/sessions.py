"""Agent sessions API: start and list the capability records that scope a Full
Brain chat (docs/reference/ASSISTANT.md "Session capabilities").

Owner-only. A session selects a read scope (domains × subjects); /chat then runs
its tools narrowed to that scope via the owner_scoped firewall. Managing sessions
runs as the full-scope owner — the narrowing applies to a session's tool reads,
not to the session list.
"""

from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jbrain.agent.agents import DEFAULT_AGENT, OWNER_AGENTS, is_owner_agent
from jbrain.agent.session import AgentSessionInfo, AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for

router = APIRouter(prefix="/sessions", dependencies=[Depends(owner_only)])


def get_agent_sessions(request: Request) -> AgentSessionRepo:
    return cast(AgentSessionRepo, request.app.state.agent_sessions)


def get_agent_transcript(request: Request) -> AgentTranscript:
    return cast(AgentTranscript, request.app.state.agent_transcript)


class SessionCreate(BaseModel):
    # The selected read scope: domain codes the session may read. Empty means the
    # owner's default — but the UI always sends an explicit, least-privilege set.
    domain_scopes: list[str] = Field(default_factory=list)
    subject_ids: list[str] = Field(default_factory=list)
    title: str = ""
    # The selected agent persona (docs/reference/ASSISTANT.md "Agent selection"); validated
    # against the closed set before it is stored.
    agent: str = DEFAULT_AGENT


class SessionOut(BaseModel):
    id: str
    title: str
    status: str
    agent: str
    domain_scopes: list[str]
    subject_ids: list[str]
    created_at: datetime
    last_active_at: datetime
    # Chats-card metadata (0/"" outside the list view).
    turn_count: int = 0
    preview: str = ""
    staged_count: int = 0
    # Sub-agent nesting (docs/SUBAGENT_SPAWNING_PLAN.md Wave S4): a child carries its
    # parent's id (so the PWA nests it and drops it from top-level bucketing); a
    # parent carries how many direct children it spawned (the rail count).
    parent_session_id: str | None = None
    subagent_count: int = 0
    # The latest run's status (running | done | error) — drives the nested rail's
    # per-child outcome glyph and the parent's failed roll-up.
    last_run_status: str | None = None
    # The last completed turn's context fill + window, so the composer's context-usage
    # meter restores when the owner reopens a chat (null until a turn reports usage).
    context_tokens: int | None = None
    context_window: int | None = None


def session_out(info: AgentSessionInfo) -> SessionOut:
    return SessionOut(
        id=info.id,
        title=info.title,
        status=info.status,
        agent=info.agent,
        domain_scopes=list(info.domain_scopes),
        subject_ids=list(info.subject_ids),
        created_at=info.created_at,
        last_active_at=info.last_active_at,
        turn_count=info.turn_count,
        preview=info.preview,
        staged_count=info.staged_count,
        parent_session_id=info.parent_session_id,
        subagent_count=info.subagent_count,
        last_run_status=info.last_run_status,
        context_tokens=info.context_tokens,
        context_window=info.context_window,
    )


@router.post("")
async def create_session(
    request: Request, principal: PrincipalDep, body: SessionCreate
) -> SessionOut:
    if not is_owner_agent(body.agent):
        raise HTTPException(
            status_code=422, detail=f"unknown agent: {body.agent!r} (one of {sorted(OWNER_AGENTS)})"
        )
    repo = get_agent_sessions(request)
    info = await repo.create(
        ctx_for(principal),
        domain_scopes=body.domain_scopes,
        subject_ids=body.subject_ids,
        title=body.title,
        agent=body.agent,
    )
    return session_out(info)


@router.get("")
async def list_sessions(request: Request, principal: PrincipalDep) -> list[SessionOut]:
    repo = get_agent_sessions(request)
    return [session_out(i) for i in await repo.list(ctx_for(principal))]


class SessionRename(BaseModel):
    title: str


@router.patch("/{session_id}")
async def rename_session(
    request: Request, principal: PrincipalDep, session_id: str, body: SessionRename
) -> Response:
    await get_agent_sessions(request).rename(ctx_for(principal), session_id, body.title)
    return Response(status_code=204)


class SessionRescope(BaseModel):
    domain_scopes: list[str]


@router.post("/{session_id}/scope")
async def rescope_session(
    request: Request, principal: PrincipalDep, session_id: str, body: SessionRescope
) -> Response:
    """Adjust a chat's read scope after start — owner-only; RLS still enforces the
    firewall on every query the session's tools run."""
    await get_agent_sessions(request).set_scopes(ctx_for(principal), session_id, body.domain_scopes)
    return Response(status_code=204)


@router.post("/{session_id}/archive")
async def archive_session(request: Request, principal: PrincipalDep, session_id: str) -> Response:
    """Tidy a chat out of the live list without deleting it (status → archived)."""
    await get_agent_sessions(request).set_status(ctx_for(principal), session_id, "archived")
    return Response(status_code=204)


@router.post("/{session_id}/unarchive")
async def unarchive_session(request: Request, principal: PrincipalDep, session_id: str) -> Response:
    """Restore an archived chat to the live list (status → active)."""
    await get_agent_sessions(request).set_status(ctx_for(principal), session_id, "active")
    return Response(status_code=204)


@router.delete("/{session_id}")
async def delete_session(request: Request, principal: PrincipalDep, session_id: str) -> Response:
    """Delete a session; its runs and transcript cascade with it."""
    await get_agent_sessions(request).delete(ctx_for(principal), session_id)
    return Response(status_code=204)


class TurnAttachmentOut(BaseModel):
    id: str
    filename: str
    media_type: str
    size_bytes: int


class TurnOut(BaseModel):
    role: str
    content: str
    # Assistant turns carry the tool steps + their note sources for the "Worked"
    # block: [{id, name, ok, sources: [{note_id, domain, snippet}]}].
    tools: list[dict[str, Any]] = Field(default_factory=list)
    # The assistant turn's reasoning trace (gpt-oss/GLM), for the "thinking"
    # disclosure; "" for user turns and non-reasoning models.
    reasoning: str = ""
    # The chat files a USER turn carried (Stage-2 attachments), replayed as chips;
    # always empty for an assistant turn.
    attachments: list[TurnAttachmentOut] = Field(default_factory=list)


@router.get("/{session_id}/transcript")
async def session_transcript(
    request: Request, principal: PrincipalDep, session_id: str
) -> list[TurnOut]:
    """Replay a session's stored conversation so reopening it shows the same chat."""
    turns = await get_agent_transcript(request).load(ctx_for(principal), session_id)
    return [
        TurnOut(
            role=t.role,
            content=t.content,
            tools=t.tools,
            reasoning=t.reasoning,
            attachments=[
                TurnAttachmentOut(
                    id=a.id,
                    filename=a.filename,
                    media_type=a.media_type,
                    size_bytes=a.size_bytes,
                )
                for a in t.attachments
            ],
        )
        for t in turns
    ]
