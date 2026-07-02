"""Best-effort event emitter for the server-brain wall display.

Two kinds of marker are POSTed to the unauthenticated on-box server-brain display
(deploy/server-brain) so it can draw a reach-out tendril:

* content-free web-tool markers — `{"kind": "web_search"|"web_fetch"}` — fired when
  jerv runs a web tool; these carry NO owner data, only the fact that a tool ran;
* opt-in LLM text markers — `{"kind": "llm_input"|"llm_output", "text": ...}` — the
  real prompt / answer text, streamed along the tendril with a fade-out popup of the
  answer. These DO carry owner data, so the caller only emits them when the owner has
  turned on the `brain_llm_stream` setting AND the display is bound to the box's own
  monitor. This module stays a dumb best-effort transport: it does not gate on the
  setting (that is the caller's job) — it only bounds the text length so a huge prompt
  can't flood the display.

All of it is fire-and-forget *display telemetry*: a failure (display down, slow,
disabled) never touches the tool result or the turn. The POST stays on-box (the api →
server-brain on the internal docker network), so it is not an egress under invariant #9.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import httpx


class BrainEmit(Protocol):
    """The wall-display emitter: `emit(kind)` for content-free web-tool markers,
    `emit(kind, text)` for the opt-in LLM prompt/answer streams."""

    def __call__(self, kind: str, text: str | None = ...) -> None: ...

# Cap the text we ship per event: enough to read at a glance on the wall, but bounded so
# a long prompt/answer can't bloat the POST or the display's buffer. Truncated with an
# ellipsis on the far side (the display shows a streaming excerpt, not the full turn).
_MAX_TEXT = 600


async def _post_event(url: str, kind: str, text: str | None = None) -> None:
    payload: dict[str, str] = {"kind": kind}
    if text:
        payload["text"] = text[:_MAX_TEXT]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(url, json=payload)
    except Exception:  # noqa: BLE001 — display telemetry must never raise into a turn
        pass


def build_event_emitter(url: str) -> BrainEmit:
    """Return `emit(kind, text=None)` that fires a fire-and-forget POST to the display,
    or a no-op when no URL is configured (the default — the display is optional). `text`
    is only meaningful for the LLM kinds; the web-tool kinds pass none."""

    def emit(kind: str, text: str | None = None) -> None:
        if not url:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (shouldn't happen inside a tool) — skip silently
        loop.create_task(_post_event(url, kind, text))

    return emit
