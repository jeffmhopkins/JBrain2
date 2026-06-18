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
import uuid
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
from jbrain.agent.skills import SkillService, format_skills
from jbrain.agent.titler import SessionTitler
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.api.settings import get_settings_store
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.llm import AssistantMessage, LlmMessage, LlmRouter, UserMessage
from jbrain.locations import LocationToolRefusal, SqlLocationRepo
from jbrain.locations.presence import presence_block, read_owner_presence

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
    # An appointment the owner is asking about, handed from the calendar. The id
    # rides as a turn-local hint so the agent resolves the exact appointment
    # (read_appointment) instead of guessing by title — it never reaches the
    # persisted transcript, which records `message` verbatim.
    appointment_id: str | None = None


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


def get_skill_service(request: Request) -> SkillService:
    return cast(SkillService, request.app.state.skill_service)


def get_agent_transcript(request: Request) -> AgentTranscript:
    return cast(AgentTranscript, request.app.state.agent_transcript)


def get_location_repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def get_device_repo(request: Request) -> SqlDeviceRepo:
    return cast(SqlDeviceRepo, request.app.state.device_repo)


async def _presence_block(
    request: Request, owner_ctx: SessionContext, session: AgentSessionInfo
) -> str:
    """The data-framed owner-presence line to prepend to the conversation (L7b), or
    "" when none. Owner-GATED and freshness-honest, mirroring the skills block's
    data/instruction boundary (a prepended data-framed `UserMessage`, NOT the system
    prompt — `run_stream` hardcodes `system=SYSTEM_PROMPT`, so a system injection
    would silently no-op in streaming).

    Two gates make it absent for a narrowed/non-owner session: the session must hold
    the `location` scope (a health-only chat gets nothing), and the read itself runs
    under the FULL owner ctx through `read_owner_presence`, which calls
    `require_full_owner`. Best-effort — a presence read failure never breaks a turn,
    it just injects no line."""
    if "location" not in session.domain_scopes:
        return ""
    try:
        presence = await read_owner_presence(
            get_location_repo(request), get_device_repo(request), owner_ctx
        )
    except LocationToolRefusal:
        # The session is not a full owner for location — inject nothing (the gate
        # held; presence is simply unavailable here).
        return ""
    except Exception as exc:  # noqa: BLE001 - a presence read hiccup must not break the turn
        log.warning("agent.presence_failed", error=repr(exc))
        return ""
    return presence_block(presence)


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


async def _maybe_autotitle(
    request: Request,
    owner_ctx: SessionContext,
    sessions: AgentSessionRepo,
    session: AgentSessionInfo,
    question: str,
    answer_parts: list[str],
) -> None:
    """Name a chat the owner left untitled, from its first exchange. Owner-only
    metadata, best-effort: a failed or empty title leaves the chat untitled (the
    UI shows a placeholder) and never breaks the turn that produced it."""
    if session.title.strip():
        return
    with contextlib.suppress(Exception):
        title = await SessionTitler(get_llm_router(request)).title_for(
            question=question, answer="".join(answer_parts)
        )
        if title:
            await sessions.rename(owner_ctx, session.id, title)


def _appt_hint(appointment_id: str | None) -> str | None:
    """A calendar handoff's appointment id, validated as a UUID before it rides
    into the prompt. Anything malformed is dropped rather than pasted in — the
    field is owner-supplied and goes straight to the model."""
    if not appointment_id:
        return None
    try:
        return str(uuid.UUID(appointment_id))
    except ValueError:
        return None


def _model_message(body: ChatRequest) -> str:
    """The model-facing user turn. A calendar handoff appends the appointment id
    as an explicit instruction so the agent reads that exact appointment rather
    than re-deriving it from the title; `body.message` stays clean for the
    transcript and the episodic trace."""
    appt_id = _appt_hint(body.appointment_id)
    if appt_id is None:
        return body.message
    return (
        f"{body.message}\n\n"
        f"(The owner is asking about the appointment with id={appt_id}. "
        "Call read_appointment with this id before answering or staging any change.)"
    )


def _conversation(body: ChatRequest) -> list[LlmMessage]:
    messages: list[LlmMessage] = [
        UserMessage(text=m.content) if m.role == "user" else AssistantMessage(text=m.content)
        for m in body.history
    ]
    messages.append(UserMessage(text=_model_message(body)))
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
    # Loop 2: surface matching active skills as a DATA-framed reference block in the conversation
    # channel (never the system prompt — the data/instruction boundary). Off by default until
    # distillation + owner promotion populate active skills; recall is RLS-scoped (in-scope only).
    if await get_settings_store(request).skills_enabled(read_ctx):
        hits = await get_skill_service(request).recall(read_ctx, body.message)
        if hits:
            conversation = [UserMessage(text=format_skills(hits)), *conversation]
            await runlog.stamp_skill_version(
                owner_ctx, run_id, skill_version=",".join(f"{h.name}@v{h.version}" for h in hits)
            )
    # L7b: prepend the owner's coarse presence as a DATA-framed UserMessage, exactly
    # like the skills block above (same data/instruction boundary) — NOT the system
    # prompt (run_stream hardcodes SYSTEM_PROMPT, so a system injection would no-op in
    # streaming). Owner-gated: absent unless the session holds the `location` scope,
    # and the read runs under the FULL owner ctx (require_full_owner), so a narrowed
    # session never gets a presence line. Names + times only, freshness-honest.
    presence = await _presence_block(request, owner_ctx, session)
    if presence:
        conversation = [UserMessage(text=presence), *conversation]
    # The owner's display zone so the agent's time prose matches the cards (which
    # the client localizes); None = UTC. Read on the owner ctx, not the narrowed
    # read ctx — a preference, not domain data.
    owner_tz = await get_settings_store(request).owner_timezone(owner_ctx)
    # Reflexion mode gate (Track R): default verify-and-annotate; this opts into
    # the buffer-then-retry path (off by default — a spinner-latency tradeoff).
    buffer_retry = await get_settings_store(request).reflexion_buffer_retry(owner_ctx)

    async def events() -> AsyncIterator[bytes]:
        stop_reason = "error"
        status = "error"
        answer: list[str] = []
        # Tool steps in call order, each gaining its sources when the result lands —
        # the assistant turn's "Worked" block, persisted with the transcript.
        steps: dict[str, dict] = {}
        order: list[str] = []
        try:
            async for event in loop.run_stream(
                session=read_ctx,
                scopes=session.domain_scopes,
                conversation=conversation,
                timezone=owner_tz,
                buffer_retry=buffer_retry,
                agent_session_id=session.id,
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
                        # Persist the staged-proposal and resolved-entity chips too,
                        # so the bubble replays in full on reopen (not just sources).
                        if event.proposal is not None:
                            step["proposal"] = event.proposal.model_dump()
                        if event.entities:
                            step["entities"] = [e.model_dump() for e in event.entities]
                elif event.type == "tool_view":
                    # Persist the rich view (e.g. a list_card) on its tool step so
                    # the bubble's tool-result views replay on reopen.
                    step = steps.get(event.tool_call_id)
                    if step is not None:
                        step["view"] = event.view.model_dump()
                elif event.type == "done":
                    stop_reason, status = event.stop_reason, "done"
                # A reflexion `verdict` rides after `done` (Loop 1's annotation of a
                # critique-worthy turn). It is forwarded to the PWA but deliberately
                # NOT recorded — Loop 1 is ephemeral and writes nothing durable.
                yield f"data: {event.model_dump_json()}\n\n".encode()
            if status == "done":
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
                await _maybe_autotitle(request, owner_ctx, sessions, session, body.message, answer)
        except asyncio.CancelledError:
            # The client disconnected mid-stream (closed the PWA, lost signal) — the turn never
            # completed, so it closes as `error` (the constraint-valid terminal; the runs table
            # carries only running/done/error, mirrored by the frontend RunStatus). The benign
            # disconnect nuance is preserved in stop_reason, not the status. Re-raise so the task
            # unwinds normally.
            status, stop_reason = "error", "disconnected"
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
