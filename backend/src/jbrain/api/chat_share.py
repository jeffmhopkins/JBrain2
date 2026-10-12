"""The PUBLIC chat share surface — unauthenticated by design (the token is the credential), the
read side of the owner mint/list/revoke on the sessions router.

A visitor GETs `/chat-share/{token}`: the token resolves to a live link, then the chat is read
PINNED to the link id so the `*_share` policies (0238) admit exactly that chat and nothing
else. Each load reads the chat as it stands, so turns the owner adds later show on the next
load. Revoked or unknown tokens 404. Every response is rate-limited, marked `no-referrer` (a
clicked citation must not leak the token via `Referer`) and `noindex`. This router carries NO
owner dependency — do not add owner-only reads here.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime symbol
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent import chat_shares
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.api.agent import serve_favicon
from jbrain.api.client_ip import client_ip
from jbrain.api.sessions import TurnOut, turn_out
from jbrain.locations.ratelimit import TokenBucket
from jbrain.web.favicon import normalize_host

router = APIRouter()


def get_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.chat_share_rate_limiter)


def get_favicon_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.chat_share_favicon_rate_limiter)


LimiterDep = Annotated[TokenBucket, Depends(get_limiter)]
FaviconLimiterDep = Annotated[TokenBucket, Depends(get_favicon_limiter)]


class SharedChatOut(BaseModel):
    """A shared chat: its header and every turn, in the owner's own transcript shape."""

    title: str
    agent: str
    last_active_at: datetime
    turns: list[TurnOut]


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


def _harden(response: Response) -> None:
    # The transcript embeds external citation URLs; a click must not send the token in the
    # Referer, and crawlers must not index the token URL.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    # Every load is the chat as it stands now; a cached copy would hide newer turns.
    response.headers["Cache-Control"] = "no-store"


def _rate_limit(request: Request, limiter: TokenBucket) -> None:
    client = client_ip(request) or "unknown"
    if not limiter.allow(client):
        raise HTTPException(status_code=429, detail="rate limited")


@router.get("/chat-share/{token}")
async def view_chat_share(
    token: str, request: Request, response: Response, limiter: LimiterDep
) -> SharedChatOut:
    """Resolve a share link to its chat as of this load. 404 on an unknown / revoked token."""
    _rate_limit(request, limiter)
    _harden(response)
    maker = _maker(request)
    resolved = await chat_shares.resolve_share(maker, token, count_view=True)
    if resolved is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    link_id, session_id = resolved
    transcript = cast(AgentTranscript, request.app.state.agent_transcript)
    chat = await chat_shares.read_shared_chat(maker, transcript, link_id, session_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    return SharedChatOut(
        title=chat.title,
        agent=chat.agent,
        last_active_at=chat.last_active_at,
        turns=[turn_out(t) for t in chat.turns],
    )


@router.get("/chat-share/{token}/favicon")
async def chat_share_favicon(
    token: str, host: str, request: Request, limiter: FaviconLimiterDep
) -> Response:
    """A citation favicon for the shared chat — only for a host one of its tool steps
    reached, so the token can't turn the box into a fetcher for arbitrary sites."""
    _rate_limit(request, limiter)
    maker = _maker(request)
    resolved = await chat_shares.resolve_share(maker, token)
    normalized = normalize_host(host)
    if resolved is None or not normalized:
        raise HTTPException(status_code=404, detail="no favicon")
    hosts = await chat_shares.shared_hosts(maker, resolved[0])
    # The source card strips a leading www. before asking, so accept either spelling.
    if normalized not in hosts and f"www.{normalized}" not in hosts:
        raise HTTPException(status_code=404, detail="no favicon")
    response = await serve_favicon(request, normalized)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
