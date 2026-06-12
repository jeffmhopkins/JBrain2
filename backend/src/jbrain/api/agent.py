"""POST /api/chat — the Full Brain conversation, streamed as SSE.

One request runs one agent turn-loop over the session's selected read scope: the
loop's ChatEvents (`text_delta`, `tool_call`, `tool_result`, `done`) are
serialized as `data:`-framed SSE so the PWA shows tool activity and the answer
live (docs/ASSISTANT.md "Streaming to the phone").

Two RLS contexts ride the request: the loop's *tool reads* run under the session
narrowed to its domains (the owner_scoped firewall), while the *run log* and the
*transcript* are owner-only — owner metadata, not in-scope content. A completed
exchange is persisted to the transcript (text + the tool sources it surfaced) so
reopening the session replays it; the sources are pointers (note id + snippet),
never copied note bodies.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jbrain.agent.loop import SYSTEM_VERSION, AgentLoop
from jbrain.agent.memory import MemoryService
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionInfo, AgentSessionRepo, read_context
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.llm import AssistantMessage, LlmMessage, LlmRouter, UserMessage

log = structlog.get_logger()

router = APIRouter(dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]


class ChatMessageIn(BaseModel):
    """A prior conversation turn the client replays for context. Only the text is
    carried — tool calls live inside a single turn-loop, not across them."""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    history: list[ChatMessageIn] = Field(default_factory=list)


def get_agent_sessions(request: Request) -> AgentSessionRepo:
    return cast(AgentSessionRepo, request.app.state.agent_sessions)


def get_agent_runlog(request: Request) -> AgentRunLog:
    return cast(AgentRunLog, request.app.state.agent_runlog)


def get_agent_registry(request: Request) -> ToolRegistry:
    return cast(ToolRegistry, request.app.state.agent_registry)


def get_llm_router(request: Request) -> LlmRouter:
    return cast(LlmRouter, request.app.state.llm_router)


def get_agent_memory(request: Request) -> MemoryService:
    return cast(MemoryService, request.app.state.agent_memory)


def get_agent_transcript(request: Request) -> AgentTranscript:
    return cast(AgentTranscript, request.app.state.agent_transcript)


async def _record_episode(
    request: Request,
    read_ctx: SessionContext,
    session: AgentSessionInfo,
    run_id: str,
    question: str,
    answer_parts: list[str],
) -> None:
    """Auto-append the turn as an episodic trace (the auto episodic tier). Best
    effort and fail-closed: it runs under the session's own scope, so the
    classifier's stamp (the session's scopes) can only ever be written and recalled
    within that firewall, and a write failure never breaks the response."""
    answer = "".join(answer_parts).strip()
    body = f"Asked: {question}" + (f"\nAnswered: {answer}" if answer else "")
    with contextlib.suppress(Exception):
        await get_agent_memory(request).record_episode(
            read_ctx,
            body=body,
            session_scopes=session.domain_scopes,
            session_id=session.id,
            run_id=run_id,
        )


async def _record_transcript(
    request: Request,
    owner_ctx: SessionContext,
    session: AgentSessionInfo,
    run_id: str,
    question: str,
    answer_parts: list[str],
    tools: list[dict],
) -> None:
    """Persist the completed exchange so the session replays on reopen. Owner-only
    (the transcript is owner metadata) and best-effort — never breaks a turn."""
    with contextlib.suppress(Exception):
        await get_agent_transcript(request).record_exchange(
            owner_ctx,
            session_id=session.id,
            run_id=run_id,
            user_text=question,
            assistant_text="".join(answer_parts),
            tools=tools,
        )


def _conversation(body: ChatRequest) -> list[LlmMessage]:
    messages: list[LlmMessage] = [
        UserMessage(text=m.content) if m.role == "user" else AssistantMessage(text=m.content)
        for m in body.history
    ]
    messages.append(UserMessage(text=body.message))
    return messages


class _RunTally:
    """Wraps the run-log recorder to total steps and cost for the run summary —
    run_stream yields ChatEvents, not the tallies, so the endpoint counts them as
    the loop records each step."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.steps = 0
        self.cost = 0

    async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
        self.steps += 1
        self.cost += cost_tokens
        await self._inner.step(  # type: ignore[attr-defined]
            idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens
        )


@router.post("/chat")
async def chat(request: Request, principal: OwnerDep, body: ChatRequest) -> StreamingResponse:
    owner_ctx = ctx_for(principal)
    sessions = get_agent_sessions(request)
    session = await sessions.get(owner_ctx, body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")

    runlog = get_agent_runlog(request)
    run_id = await runlog.start(owner_ctx, session_id=session.id, prompt_version=SYSTEM_VERSION)
    await sessions.touch(owner_ctx, session.id)

    tally = _RunTally(runlog.bound(owner_ctx, run_id))
    loop = AgentLoop(get_llm_router(request), get_agent_registry(request), recorder=tally)
    read_ctx = read_context(principal.id, session.domain_scopes)
    conversation = _conversation(body)

    async def events() -> AsyncIterator[bytes]:
        stop_reason = "error"
        status = "failed"
        answer: list[str] = []
        # Tool steps in call order, each gaining its sources when the result lands —
        # the assistant turn's "Worked" block, persisted with the transcript.
        steps: dict[str, dict] = {}
        order: list[str] = []
        try:
            async for event in loop.run_stream(
                session=read_ctx, scopes=session.domain_scopes, conversation=conversation
            ):
                if event.type == "text_delta":
                    answer.append(event.text)
                elif event.type == "tool_call":
                    steps[event.id] = {
                        "id": event.id,
                        "name": event.name,
                        "ok": None,
                        "sources": [],
                    }
                    order.append(event.id)
                elif event.type == "tool_result":
                    step = steps.get(event.tool_call_id)
                    if step is not None:
                        step["ok"] = event.ok
                        step["sources"] = [s.model_dump() for s in event.sources]
                elif event.type == "done":
                    stop_reason, status = event.stop_reason, "ended"
                yield f"data: {event.model_dump_json()}\n\n".encode()
            if status == "ended":
                await _record_episode(request, read_ctx, session, run_id, body.message, answer)
                await _record_transcript(
                    request,
                    owner_ctx,
                    session,
                    run_id,
                    body.message,
                    answer,
                    [steps[i] for i in order],
                )
        except asyncio.CancelledError:
            # The client disconnected mid-stream (closed the PWA, lost signal) —
            # a benign abort, not a failure. Record it as such, then re-raise so
            # the task unwinds normally.
            status, stop_reason = "cancelled", "disconnected"
            raise
        except Exception as exc:  # noqa: BLE001 — surface a terminal event, never a 500 mid-stream
            log.warning("agent.chat_failed", run_id=run_id, error=repr(exc))
            yield b'data: {"type": "done", "stop_reason": "error"}\n\n'
        finally:
            # Shield the closing UPDATE so a disconnect-driven cancellation can't
            # interrupt it and strand the run in 'running'; suppress so a write
            # failure never masks the real outcome (recording must not break a turn).
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    runlog.finish(
                        owner_ctx,
                        run_id,
                        status=status,
                        stop_reason=stop_reason,
                        step_count=tally.steps,
                        cost_tokens=tally.cost,
                    )
                )

    # X-Accel-Buffering off so nginx streams events instead of buffering the turn.
    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
