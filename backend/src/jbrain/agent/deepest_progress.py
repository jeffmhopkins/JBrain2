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
from collections.abc import Awaitable, Callable, Sequence
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


def _deepest_view_step(data: dict[str, Any]) -> dict[str, Any]:
    """A persisted tool step carrying the `deepest_run` tool-view, so the progress turn
    replays as the backgrounded deep_research timeline card on reopen (R8; the same
    `step["view"]` shape the transcript accumulator writes for a live tool-view). Data-only
    — the payload is the run's checkpoint state, never model prose."""
    return {
        "name": "deepest_research",
        "args": {},
        "ok": True,
        "summary": "",
        # `sources` is REQUIRED: the transcript accumulator seeds every persisted tool step
        # with `sources: []`, and the PWA's transcript hydrator reads `tool.sources` un-guarded
        # (useFullBrain.fromTurn) — a hand-built step that omits it throws on reopen and blanks
        # the whole session. Keep this shape in lockstep with the accumulator's canonical step.
        "sources": [],
        "view": {"view": "deepest_run", "surface": "inline", "data": data, "refs": []},
    }


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
        # A run outlives many token changes, so a static token list captured at build time
        # goes stale (a device registered after boot never gets poked). The provider resolves
        # the owner's live tokens per tick instead; `push_tokens` stays for tests.
        push_tokens_provider: Callable[[], Awaitable[Sequence[str]]] | None = None,
    ) -> None:
        self._transcript = transcript
        self._notify = notify
        self._push = push
        self._push_tokens = list(push_tokens)
        self._push_tokens_provider = push_tokens_provider

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
        # The card advances coarsely per round: round 1 is the gather stage, later rounds
        # the gap-fill stage (the timeline's active step).
        view = _deepest_view_step(
            {
                "round": round_no,
                "sources": findings,
                "coverage": coverage_label,
                "status": "running",
                "step": 2 if round_no <= 1 else 5,
                "label": body,
            }
        )
        await self._emit(
            owner_ctx,
            session_id=session_id,
            run_id=run_id,
            title="Deepest research — in progress",
            body=body,
            view=view,
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
        view = _deepest_view_step({"status": "done", "step": 8, "label": body})
        await self._emit(
            owner_ctx,
            session_id=session_id,
            run_id=run_id,
            title="Deepest research ready",
            body=body,
            view=view,
        )

    async def _emit(
        self,
        owner_ctx: SessionContext,
        *,
        session_id: str,
        run_id: str,
        title: str,
        body: str,
        view: dict[str, Any] | None = None,
    ) -> None:
        # (1) durable: append the server-authored assistant turn (renders on reopen). The
        # `deepest_run` tool-view rides the turn's `tools` so it replays as the card on load.
        # run_id is None here on purpose: `agent_turns.run_id` is an `app.runs` UUID FK, but a
        # deepest lane `run_id` is "deepest-<uuid>" (the run-state key, text) — passing it made
        # `record_answer`'s `uuid.UUID(run_id)` raise, silently dropping EVERY progress turn.
        # These are server-authored ticks with no agent run, so None is the honest value.
        if self._transcript is not None:
            try:
                await self._transcript.record_answer(
                    owner_ctx,
                    session_id=session_id,
                    run_id=None,
                    assistant_text=body,
                    tools=[view] if view is not None else [],
                )
            except Exception:  # noqa: BLE001 — progress is best-effort, never crash the run
                log.warning("deepest_progress.record_failed", run_id=run_id, exc_info=True)
        # (2) nudge: NotifyBus (ref=session_id deep-link) — notify_owner already swallows.
        notify_owner(
            self._notify,
            Notification(kind=NOTIFY_KIND, title=title, body=_clip(body), ref=session_id),
        )
        # (3) wake a closed app: an FCM content-free poke (no PII in the push). Tokens come
        # from the static list (tests) or the live provider (prod) — resolved per tick so a
        # newly-registered device still gets woken.
        if self._push is not None:
            tokens = list(self._push_tokens)
            if not tokens and self._push_tokens_provider is not None:
                with contextlib.suppress(Exception):
                    tokens = list(await self._push_tokens_provider())
            if tokens:
                with contextlib.suppress(Exception):
                    await self._push.poke(tokens)
