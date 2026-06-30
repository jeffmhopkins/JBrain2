"""Best-effort web-tool event emitter for the server-brain wall display.

When jerv runs `web_search` / `web_fetch`, we POST a tiny `{"kind": ...}` marker to
the unauthenticated on-box server-brain display (deploy/server-brain) so it can
draw a reach-out tendril. This is fire-and-forget *display telemetry*: it carries
no owner data — only the fact that a web tool ran — and a failure (display down,
slow, disabled) never touches the tool result or the turn. The POST stays on-box
(the api → server-brain on the internal docker network), so it is not an egress
under invariant #9.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx


async def _post_event(url: str, kind: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(url, json={"kind": kind})
    except Exception:  # noqa: BLE001 — display telemetry must never raise into a turn
        pass


def build_event_emitter(url: str) -> Callable[[str], None]:
    """Return `emit(kind)` that fires a fire-and-forget POST to the display, or a
    no-op when no URL is configured (the default — the display is optional)."""

    def emit(kind: str) -> None:
        if not url:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (shouldn't happen inside a tool) — skip silently
        loop.create_task(_post_event(url, kind))

    return emit
