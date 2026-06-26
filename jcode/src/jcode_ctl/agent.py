"""The coding-agent port: stream a turn over a workspace.

The control server depends on this small interface, never on the Claude Agent
SDK directly, so the session logic and the HTTP surface are unit-testable with a
fake (no SDK, no model gateway, no network). The real adapter
(:class:`ClaudeCodeAgent`) lazy-imports ``claude_agent_sdk`` — installed in the
image, not in dev/CI — and is exercised only on-box.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# The turn-event vocabulary mirrors the /chat SSE frames (docs/ASSISTANT.md
# "Streaming to the phone") so the api can map jcode turns onto the SAME
# ChatEvent contract the PWA already renders (Wave J2).
EventType = Literal["text", "tool_use", "tool_result", "done", "error"]


@dataclass(frozen=True)
class TurnEvent:
    """One streamed step of a coding turn."""

    type: EventType
    text: str = ""
    # Tool name for tool_use/tool_result (e.g. "Edit", "Bash"); empty otherwise.
    tool: str = ""
    # Free-form structured payload (a command, a diff stat, an error detail).
    data: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class CodingAgent(Protocol):
    """Drives one coding turn in ``cwd`` and streams its events.

    ``session_id`` lets an implementation resume the underlying agent session
    (the SDK's resumable ``session_id``); ``cwd`` is the per-session checkout.
    """

    def run_turn(
        self, session_id: str, prompt: str, cwd: str, *, model: str = ""
    ) -> AsyncIterator[TurnEvent]: ...

    async def cancel(self, session_id: str) -> None: ...


class FakeCodingAgent:
    """A scripted agent for tests: deterministic events, no SDK, no network."""

    def __init__(self, script: list[TurnEvent] | None = None) -> None:
        self._script = script
        self.cancelled: list[str] = []
        # The model passed to each run_turn, in order — tests assert the session's
        # selected model reaches the agent.
        self.models: list[str] = []

    async def run_turn(
        self, session_id: str, prompt: str, cwd: str, *, model: str = ""
    ) -> AsyncIterator[TurnEvent]:
        self.models.append(model)
        events = self._script or [
            TurnEvent("text", text="Reading the file and planning the change."),
            TurnEvent("tool_use", tool="Read", data={"command": "read src/app.ts"}),
            TurnEvent("tool_result", tool="Read", data={"ok": True}),
            TurnEvent("text", text="Done — applied the change in the sandbox."),
            TurnEvent("done"),
        ]
        for ev in events:
            yield ev

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class ClaudeCodeAgent:
    """Real adapter over the Claude Agent SDK (the Claude Code engine, headless).

    Lazy-imports ``claude_agent_sdk`` so the package's absence never breaks the
    service build or the fake-driven unit tests. The model is on-box: the SDK and
    its ``claude`` CLI both read ``ANTHROPIC_BASE_URL`` from the process
    environment (set by the image/compose to the local gateway), so no code
    leaves the box.

    NOTE (on-box verification — JCODE_PLAN.md open decision 1): the exact
    SDK message → :class:`TurnEvent` mapping and the native-``/v1/messages``-vs-shim
    bridge are confirmed against real hardware on the Strix Halo box; the
    structure here is the seam those land in, kept behind this port.
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._sdk = None  # resolved on first use

    def _require_sdk(self):  # pragma: no cover - exercised only on-box
        if self._sdk is None:
            try:
                import claude_agent_sdk  # type: ignore
            except ModuleNotFoundError as exc:  # pragma: no cover
                raise RuntimeError(
                    "claude_agent_sdk is not installed — the jcode image provides it; "
                    "this adapter only runs on the box."
                ) from exc
            self._sdk = claude_agent_sdk
        return self._sdk

    async def run_turn(  # pragma: no cover - exercised only on-box
        self, session_id: str, prompt: str, cwd: str, *, model: str = ""
    ) -> AsyncIterator[TurnEvent]:
        sdk = self._require_sdk()
        # The per-session model (from the owner's selection) overrides the adapter's
        # construction-time default; both name a served model on the on-box gateway.
        active_model = model or self._model
        # Mapping SDK message stream → TurnEvent lands here once verified on-box.
        # Sketch: drive sdk.query(...) with options pinning cwd=cwd, active_model,
        # and a resumable session id, then translate each message.
        raise NotImplementedError(
            "ClaudeCodeAgent.run_turn is wired and verified on the box (Wave J1 "
            "on-box smoke test); unit tests use FakeCodingAgent."
        )
        # Unreachable, but documents the contract for the on-box wiring:
        if False:
            yield TurnEvent("done")
        _ = (sdk, session_id, prompt, cwd, active_model)

    async def cancel(self, session_id: str) -> None:  # pragma: no cover - on-box
        raise NotImplementedError
