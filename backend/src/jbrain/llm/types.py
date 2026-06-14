"""Provider-agnostic request/response types and the LlmClient protocol."""

import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

DEFAULT_MAX_TOKENS = 4096

# Models love wrapping JSON in markdown fences; tolerating that here avoids a
# pointless re-ask round trip.
_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)


@dataclass(frozen=True)
class LlmImage:
    """One base64-encoded image for vision tasks."""

    media_type: str
    data: str


@dataclass(frozen=True)
class LlmUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LlmResult:
    """Adapter response: raw text always; `parsed` only when a JSON schema was
    requested and the text parsed (None signals the router to re-ask)."""

    text: str
    parsed: Any | None
    usage: LlmUsage


class UsageRecorder(Protocol):
    """Persists one call's token usage (docs/ANALYSIS.md "Token accounting").

    A protocol rather than a concrete class keeps the llm package free of any
    persistence dependency; the SQL implementation lives in jbrain.usage. The
    router invokes it fire-and-forget — implementations may raise, the call
    must still succeed.
    """

    async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None: ...


# --- Tool-using (agentic) conversations ------------------------------------
#
# `complete` is single-shot text/JSON. The agent loop needs a multi-turn,
# tool-aware surface: the model may answer or request tool calls, the loop runs
# them and feeds the results back, repeating until the model stops. These types
# are the provider-agnostic shape of that exchange; the per-provider clients map
# them onto Anthropic content blocks / OpenAI tool_calls.


@dataclass(frozen=True)
class LlmTool:
    """A tool the model may call: a name, a description it reads, and a JSON
    Schema for the arguments. The agent assembles these from `.tool` sidecars."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A model request to invoke a tool (Anthropic `tool_use` / OpenAI
    `tool_calls`). `id` ties the eventual result back to this request."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The outcome of running a ToolCall, fed back to the model on the next turn.
    `is_error` marks a failed call so the model can self-correct rather than
    treating the message as a successful observation."""

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class UserMessage:
    """Owner/user input for a turn, with optional vision images."""

    text: str
    images: Sequence[LlmImage] = ()


@dataclass(frozen=True)
class AssistantMessage:
    """A prior assistant turn: any text it produced plus the tool calls it made.
    Replayed back so the model sees its own tool requests in context."""

    text: str = ""
    tool_calls: Sequence[ToolCall] = ()


@dataclass(frozen=True)
class ToolResultMessage:
    """The results of the tool calls from the preceding assistant turn."""

    results: Sequence[ToolResult]


LlmMessage = UserMessage | AssistantMessage | ToolResultMessage

# Why the model stopped: it finished its turn, it wants tools run, or it hit the
# token ceiling. Providers' own reasons are normalized onto these three.
StopReason = Literal["end_turn", "tool_use", "max_tokens"]


@dataclass(frozen=True)
class LlmTurn:
    """One assistant turn from a tool-aware completion: its text, the tool calls
    it requested (empty unless `stop_reason == "tool_use"`), and usage."""

    text: str
    tool_calls: Sequence[ToolCall]
    stop_reason: StopReason
    usage: LlmUsage


@dataclass(frozen=True)
class TextChunk:
    """One incremental slice of streamed assistant text. The loop forwards these
    to the phone as `text_delta` events so the answer renders token-by-token."""

    text: str


# A streamed turn is a sequence of incremental TextChunks followed by exactly one
# final LlmTurn: text streams live, while tool calls are assembled whole (their
# arguments arrive as fragments and are only valid once complete) and carried on
# the closing turn alongside the full text, stop reason, and usage.
StreamPart = TextChunk | LlmTurn


class LlmClient(Protocol):
    """One provider's completion surface. All application code routes through
    LlmRouter; this protocol exists so tests and the router can swap providers
    (or a fake) freely."""

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
    ) -> LlmResult: ...

    async def converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
    ) -> LlmTurn: ...

    def converse_stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamPart]:
        """Stream one tool-aware turn: incremental TextChunks then one final
        LlmTurn (see StreamPart). An async generator, so it is declared — not
        `async def` — returning the iterator the caller drives with `async for`."""
        ...


def parse_json_payload(text: str) -> Any | None:
    """Parse model output as JSON, tolerating markdown fences; None on failure."""
    candidate = text.strip()
    match = _FENCE.match(candidate)
    if match:
        candidate = match.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None
