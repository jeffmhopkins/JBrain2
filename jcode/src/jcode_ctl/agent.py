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
from typing import Any, Literal, Protocol, runtime_checkable

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

    def forget(self, session_id: str) -> None:
        """Drop any per-session adapter state — called when a session is deleted."""
        ...


class FakeCodingAgent:
    """A scripted agent for tests: deterministic events, no SDK, no network."""

    def __init__(self, script: list[TurnEvent] | None = None) -> None:
        self._script = script
        self.cancelled: list[str] = []
        self.forgotten: list[str] = []
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

    def forget(self, session_id: str) -> None:
        self.forgotten.append(session_id)


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
        self._sdk: Any = None  # resolved on first use
        # Our session id -> the SDK's resumable session id, captured after a turn so
        # the next turn of the same session continues the agent's context.
        self._sdk_sessions: dict[str, str] = {}
        # Session ids with a cancel requested mid-turn — the run loop checks this at
        # each message boundary and stops (cooperative interrupt).
        self._cancelled: set[str] = set()

    def _require_sdk(self) -> Any:  # pragma: no cover - exercised only on-box
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
        self._cancelled.discard(session_id)
        # The per-session model (the owner's selection) overrides the construction
        # default; both name a served model on the on-box gateway, reached over
        # ANTHROPIC_BASE_URL. bypassPermissions: the workspace is an isolated,
        # throwaway per-session checkout on its own network (no host, no notes, no
        # other services), so the agent runs fully autonomous — there is no
        # interactive approver to prompt in this headless service. `resume` continues
        # the SDK session so multi-turn keeps context.
        options = sdk.ClaudeAgentOptions(
            cwd=cwd,
            model=model or self._model,
            permission_mode="bypassPermissions",
            resume=self._sdk_sessions.get(session_id),
            include_partial_messages=False,
        )
        result: dict[str, object] = {}
        # tool_use id -> tool name, within THIS turn: a ToolResultBlock carries only
        # tool_use_id, so we resolve the name from the tool_use that opened it. Scoped
        # to the turn (not the agent) so it never grows unbounded.
        tool_names: dict[str, str] = {}
        try:
            async for message in sdk.query(prompt=prompt, options=options):
                for ev in self._to_events(sdk, message, tool_names):
                    yield ev
                if isinstance(message, sdk.ResultMessage):
                    sid = getattr(message, "session_id", "")
                    if sid:
                        self._sdk_sessions[session_id] = sid
                    result = {
                        "status": getattr(message, "subtype", "success"),
                        "cost_usd": getattr(message, "total_cost_usd", None) or 0.0,
                    }
                if session_id in self._cancelled:
                    yield TurnEvent("error", text="cancelled")
                    break
        except Exception as exc:  # a turn must always end with a terminal frame
            yield TurnEvent("error", text=str(exc))
        finally:
            self._cancelled.discard(session_id)
        yield TurnEvent("done", data=result)

    def _to_events(  # pragma: no cover - exercised only on-box
        self, sdk: Any, message: Any, tool_names: dict[str, str]
    ) -> list[TurnEvent]:
        """Map one SDK message to zero or more TurnEvents. Defensive (the SDK's block
        shapes vary by version); finalized against real output in the on-box smoke
        test (JCODE_PLAN.md open decision 1). ``tool_names`` carries tool_use id ->
        name across this turn so a tool_result can name the tool it answers."""
        out: list[TurnEvent] = []
        if isinstance(message, sdk.AssistantMessage):
            for block in message.content:
                if isinstance(block, sdk.TextBlock):
                    out.append(TurnEvent("text", text=block.text))
                elif isinstance(block, sdk.ToolUseBlock):
                    tool_names[block.id] = block.name
                    out.append(
                        TurnEvent(
                            "tool_use",
                            tool=block.name,
                            data={"input": block.input},
                        )
                    )
        elif isinstance(message, sdk.UserMessage):
            # A UserMessage carries tool results back to the model. A ToolResultBlock
            # has only tool_use_id (no name) — resolve the name from this turn's map.
            for block in getattr(message, "content", []):
                tool_use_id = getattr(block, "tool_use_id", "")
                if tool_use_id or getattr(block, "type", "") == "tool_result":
                    out.append(
                        TurnEvent(
                            "tool_result",
                            tool=tool_names.get(tool_use_id, ""),
                            data={"ok": not getattr(block, "is_error", False)},
                        )
                    )
        elif isinstance(message, sdk.ResultMessage):
            subtype = getattr(message, "subtype", "success")
            if subtype != "success":
                out.append(TurnEvent("error", text=str(subtype)))
        return out

    async def cancel(self, session_id: str) -> None:  # pragma: no cover - on-box
        # Cooperative interrupt: the run loop stops at the next message boundary. A
        # hard mid-tool interrupt would need ClaudeSDKClient.interrupt — a follow-up.
        self._cancelled.add(session_id)

    def forget(self, session_id: str) -> None:  # pragma: no cover - on-box
        # Drop the session's resume id + any cancel flag when it's deleted, so neither
        # map grows over the life of the server.
        self._sdk_sessions.pop(session_id, None)
        self._cancelled.discard(session_id)
