"""Agent sessions API: start and list the capability records that scope a Full
Brain chat (docs/ASSISTANT.md "Session capabilities").

Owner-only. A session selects a read scope (domains × subjects); /chat then runs
its tools narrowed to that scope via the owner_scoped firewall. Managing sessions
runs as the full-scope owner — the narrowing applies to a session's tool reads,
not to the session list.
"""

from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

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


class SessionOut(BaseModel):
    id: str
    title: str
    status: str
    domain_scopes: list[str]
    subject_ids: list[str]
    created_at: datetime
    last_active_at: datetime


def session_out(info: AgentSessionInfo) -> SessionOut:
    return SessionOut(
        id=info.id,
        title=info.title,
        status=info.status,
        domain_scopes=list(info.domain_scopes),
        subject_ids=list(info.subject_ids),
        created_at=info.created_at,
        last_active_at=info.last_active_at,
    )


@router.post("")
async def create_session(
    request: Request, principal: PrincipalDep, body: SessionCreate
) -> SessionOut:
    repo = get_agent_sessions(request)
    info = await repo.create(
        ctx_for(principal),
        domain_scopes=body.domain_scopes,
        subject_ids=body.subject_ids,
        title=body.title,
    )
    return session_out(info)


@router.get("")
async def list_sessions(request: Request, principal: PrincipalDep) -> list[SessionOut]:
    repo = get_agent_sessions(request)
    return [session_out(i) for i in await repo.list(ctx_for(principal))]


class TurnOut(BaseModel):
    role: str
    content: str
    # Assistant turns carry the tool steps + their note sources for the "Worked"
    # block: [{id, name, ok, sources: [{note_id, domain, snippet}]}].
    tools: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/{session_id}/transcript")
async def session_transcript(
    request: Request, principal: PrincipalDep, session_id: str
) -> list[TurnOut]:
    """Replay a session's stored conversation so reopening it shows the same chat."""
    turns = await get_agent_transcript(request).load(ctx_for(principal), session_id)
    return [TurnOut(role=t.role, content=t.content, tools=t.tools) for t in turns]
