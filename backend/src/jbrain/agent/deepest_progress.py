"""Periodic progress from a background deepest run back into the initiating chat
(docs/plans/DEEPEST_RESEARCH_TOOL_PLAN.md, R6).

A background run has no live `/chat` turn to stream `ToolProgressEvent`s into (that SSE
broker dies with the turn), so this channel reuses the two already-proven **off-turn**
paths instead:

- **durable delivery** — each tick appends a server-authored assistant turn to the
  initiating session via `AgentTranscript.record_answer` (owner-RLS, append-only
  `agent_turns`), exactly as `tasks/runner.py` records a headless task's answer. It renders
  in the chat on the next load — no fake owner bubble (that's why `record_answer`, not
  `record_exchange`).
- **the nudge** — a `NotifyBus` notification whose `ref` is the session id (so the app
  deep-links straight to the run's chat) plus an FCM content-free `poke`, so a closed app is
  pulled back.

Emitted per round + on completion. Everything is **best-effort** — a progress/notify
failure must never crash or stall the run, so each leg swallows its own errors. Live
in-place streaming into an already-open surface between turns is deferred (§8): no
per-session standing channel exists yet.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import Any, Protocol

import structlog

from jbrain.db.session import SessionContext
from jbrain.notify import Notification, NotifyBus, notify_owner

log = structlog.get_logger()

# The notification kind the app routes on (its tap target is the run's chat via `ref`).
NOTIFY_KIND = "deepest_research"
_BODY_MAX = 200


class _Transcript(Protocol):
    async def record_answer(
        self,
        ctx: SessionContext,
        *,
        session_id: str,
        run_id: str | None,
        assistant_text: str,
        tools: Sequence[dict[str, Any]],
        reasoning: str = "",
    ) -> None: ...


class _Push(Protocol):
    async def poke(self, tokens: list[str]) -> None: ...


def _clip(text: str, n: int = _BODY_MAX) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class DeepestProgressChannel:
    """Posts a background deepest run's progress into the initiating chat and nudges the
    owner's devices. Constructed once per run (or shared) with the transcript store and the
    optional notify/push transports; a None transport degrades cleanly to a no-op."""

    def __init__(
        self,
        *,
        transcript: _Transcript | None,
        notify: NotifyBus | None = None,
        push: _Push | None = None,
        push_tokens: Sequence[str] = (),
    ) -> None:
        self._transcript = transcript
        self._notify = notify
        self._push = push
        self._push_tokens = list(push_tokens)

    async def round(
        self,
        owner_ctx: SessionContext,
        *,
        session_id: str,
        run_id: str,
        round_no: int,
        findings: int,
        coverage_label: str,
    ) -> None:
        """A per-round progress tick — the report is still being built."""
        body = (
            f"Deepest research · round {round_no} · {findings} finding(s) so far · "
            f"{coverage_label} · still going"
        )
        await self._emit(
            owner_ctx,
            session_id=session_id,
            run_id=run_id,
            title="Deepest research — in progress",
            body=body,
        )

    async def done(
        self,
        owner_ctx: SessionContext,
        *,
        session_id: str,
        run_id: str,
        question: str,
    ) -> None:
        """The completion tick — the report has landed in the library."""
        body = f"Deepest research complete — “{question}”. The report is ready."
        await self._emit(
            owner_ctx,
            session_id=session_id,
            run_id=run_id,
            title="Deepest research ready",
            body=body,
        )

    async def _emit(
        self,
        owner_ctx: SessionContext,
        *,
        session_id: str,
        run_id: str,
        title: str,
        body: str,
    ) -> None:
        # (1) durable: append the server-authored assistant turn (renders on reopen).
        if self._transcript is not None:
            try:
                await self._transcript.record_answer(
                    owner_ctx,
                    session_id=session_id,
                    run_id=run_id,
                    assistant_text=body,
                    tools=[],
                )
            except Exception:  # noqa: BLE001 — progress is best-effort, never crash the run
                log.warning("deepest_progress.record_failed", run_id=run_id, exc_info=True)
        # (2) nudge: NotifyBus (ref=session_id deep-link) — notify_owner already swallows.
        notify_owner(
            self._notify,
            Notification(kind=NOTIFY_KIND, title=title, body=_clip(body), ref=session_id),
        )
        # (3) wake a closed app: an FCM content-free poke (no PII in the push).
        if self._push is not None and self._push_tokens:
            with contextlib.suppress(Exception):
                await self._push.poke(self._push_tokens)
