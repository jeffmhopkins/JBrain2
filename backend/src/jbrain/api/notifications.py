"""The owner notifications stream — self-hosted push delivery.

The native owner app opens one SSE connection here on the owner session cookie; the
server streams `Notification` events (task-ready, ...) it can render as local device
notifications. No third party, no FCM: the owner's own device talking to the owner's own
server. Auth is the owner gate (a non-owner 403s); publishing lives in `jbrain.notify`.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jbrain.api.deps import OwnerDep
from jbrain.notify.bus import Notification, NotifyBus

log = structlog.get_logger()
router = APIRouter()

# Emit an SSE keepalive comment when no event arrives for this long, so an idle proxy
# doesn't drop the long-lived connection (matches the agent stream's keepalive intent).
_KEEPALIVE_S = 25.0


def _bus(request: Request) -> NotifyBus:
    return request.app.state.notify_bus


def sse_event(note: Notification) -> bytes:
    """One notification as an SSE `data:` frame (JSON body)."""
    payload = json.dumps(
        {"kind": note.kind, "title": note.title, "body": note.body, "ref": note.ref}
    )
    return f"data: {payload}\n\n".encode()


async def stream_notifications(
    bus: NotifyBus,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    keepalive_s: float = _KEEPALIVE_S,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE frames for one connection until the client disconnects: an opening
    comment (flushes headers past a buffering proxy), each published notification, and a
    keepalive comment on idle. Unsubscribes on exit however the loop ends. Extracted from
    the route so the loop is unit-testable without the streaming transport."""
    q = bus.subscribe()
    try:
        yield b": connected\n\n"
        while not await is_disconnected():
            try:
                note = await asyncio.wait_for(q.get(), timeout=keepalive_s)
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            yield sse_event(note)
    finally:
        bus.unsubscribe(q)


@router.get("/notifications/stream")
async def notifications_stream(request: Request, principal: OwnerDep) -> StreamingResponse:
    """Stream owner notifications as SSE until the client disconnects (owner-only)."""
    return StreamingResponse(
        stream_notifications(_bus(request), request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
