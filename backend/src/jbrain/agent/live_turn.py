"""The in-flight-turn frame buffer + live fan-out broker, shared by the /chat SSE path
and headless turns that want to stream (plan continuations).

A `_LiveTurn` is one turn's replay buffer plus a set of live subscribers. The driving
task feeds it `emit()`/`finish()`; the original SSE response AND any reconnecting client
(`GET /chat/runs/{id}/stream`) subscribe via `stream(after)` to replay the frames so far
and then follow the turn to completion. Kept in `app.state.live_turns`, keyed by run_id.

Extracted from `api/agent.py` so a headless continuation (`agent/continuation.py`) can
register a real streamable turn too — importing it here avoids the
`api/agent → tasks/runner → agent/continuation → api/agent` import cycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from jbrain.agent.transcript_accumulator import TranscriptAccumulator

# Emit an SSE keepalive when the turn streams nothing for this long, so an idle proxy
# (Cloudflare's ~100s cap over the tunnel) can't drop the connection during a long
# blocking tool — an image render's cold model-load gap (minutes with no events)
# especially, now that we free ComfyUI between renders.
_SSE_HEARTBEAT_SECONDS = 20.0

_TURN_DONE = object()  # per-subscriber sentinel: the turn finished, no more frames

# A memory backstop on one turn's live frame buffer. Set far above any real turn (a
# heavy fan streams dozens-to-low-thousands of frames); it only bounds a pathological
# runaway that streams for the whole wall-clock. Past it the oldest frames are evicted.
_MAX_BUFFERED_FRAMES = 20000


class _LiveTurn:
    """An in-flight turn's frame buffer + live fan-out, so the original SSE response AND
    a reconnecting client (GET /chat/runs/{id}/stream) can both replay the frames so far
    and follow the turn to completion. In-process, keyed by run_id; the detached
    `drive_turn` task feeds it via `emit`/`finish`. Buffered frames are the `data:` SSE
    lines only — keepalives are per-connection (emitted on idle by `stream`), never
    buffered, so a reconnect's `after` offset counts only real events."""

    def __init__(self, session_id: str = "") -> None:
        # The chat session this turn streams into. Lets the concurrency guard reject a
        # second live turn for the same session, and the rejoin lookup map a session back
        # to its live run_id — both without a DB hop. Defaults to "" (never a real session
        # id, so it matches nothing) for the buffer-only unit tests that don't set it.
        self.session_id = session_id
        self.frames: list[bytes] = []
        # Absolute index of frames[0]: count evicted off the front once the buffer hits
        # its cap, so a reconnect's `after` stays an ABSOLUTE event index (frames[0] is
        # logical frame `_base`). Without this, a runaway turn that streams tens of
        # thousands of token frames over the (up-to-1h) wall-clock grows memory unbounded.
        self._base = 0
        self.done = False
        self._subs: set[asyncio.Queue[bytes | object]] = set()
        # The driving task — held so the cancel endpoint and shutdown can stop it. Typed
        # `Any` result: /chat's drive_turn returns None, a plan continuation's turn task returns
        # an ExecutedTurn; only `.cancel()` is ever called on it, so the result type is moot.
        self.task: asyncio.Task[Any] | None = None
        # The turn's live render accumulator, set by `drive_turn` once it exists. The
        # reattach snapshot reads it so a reloaded PWA seeds its bubble from the turn's
        # render SO FAR — no dependence on the frame buffer still holding the (possibly
        # evicted) early frames of a long deep-research fan. None until the task attaches it.
        self.acc: TranscriptAccumulator | None = None

    @property
    def frame_index(self) -> int:
        """The ABSOLUTE index of the next frame — the total emitted so far (survivors plus
        the count evicted off the front). A reattaching client that seeds from the snapshot
        resumes the live stream at exactly this offset, so it neither misses a frame nor
        replays one it already has in the snapshot."""
        return self._base + len(self.frames)

    def emit(self, frame: bytes) -> None:
        """Append a data frame and fan it out to every live subscriber. INVARIANT: every
        buffered frame is exactly one client-parseable `data:` SSE event — the reconnect
        `after` offset counts events on both sides, so a frame the client's parser would
        skip (a comment, a multi-event blob) would desync it. The buffer grows for one
        turn only and is freed when the run leaves `live_turns`. No `await` between the
        append and the fan-out, so a subscriber's snapshot can never miss an interleaved
        frame. Past `_MAX_BUFFERED_FRAMES` the OLDEST frames are evicted (a memory
        backstop on a runaway fan) — a reconnect that lands before the evicted point
        rebuilds the fan from later frames (the fold lazily re-creates a child whose
        `subagent_spawned` frame is gone), so eviction degrades, never breaks, replay."""
        self.frames.append(frame)
        overflow = len(self.frames) - _MAX_BUFFERED_FRAMES
        if overflow > 0:
            del self.frames[:overflow]
            self._base += overflow
        for q in self._subs:
            q.put_nowait(frame)

    def finish(self) -> None:
        """Mark the turn complete and terminate every live subscriber. Idempotent."""
        self.done = True
        for q in self._subs:
            q.put_nowait(_TURN_DONE)
        self._subs.clear()

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    async def stream(self, after: int = 0) -> AsyncIterator[bytes]:
        """Replay buffered frames from index `after`, then follow live frames until the
        turn ends. A keepalive comment is emitted whenever no frame arrives within the
        heartbeat window, so an idle proxy can't drop a connection during a long tool.
        Backfill is synchronous (no await before the subscription is registered) so no
        frame can slip in between the snapshot and going live."""
        q: asyncio.Queue[bytes | object] = asyncio.Queue()
        # `after` is an absolute event index; translate it past any front-evicted frames.
        for frame in self.frames[max(after - self._base, 0) :]:
            q.put_nowait(frame)
        if self.done:
            q.put_nowait(_TURN_DONE)
        else:
            self._subs.add(q)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if item is _TURN_DONE:
                    return
                yield cast(bytes, item)
        finally:
            self._subs.discard(q)
