"""Provider-agnostic request/response types and the LlmClient protocol."""

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, fields
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


# Decode-time sampling controls, one immutable bundle threaded from the router to
# each provider client. EVERY field is optional (None): an unset field is left OFF
# the wire so the provider/engine default applies — the point is that a model gets
# ONLY the knobs its vendor actually recommends, never a value we invented. The
# router fills these from the resolved model's recommended defaults
# (jbrain.llm.model_sampling), a `.prompt` `config: sampling:` block overriding
# per-task. Provider quirks (Anthropic rejecting temperature+top_p together, xAI's
# reasoning models rejecting penalties, llama.cpp's non-OpenAI knob names) are the
# CLIENT's job, not this bundle's — see each client's `_apply_sampling`.
@dataclass(frozen=True)
class Sampling:
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    # llama.cpp's `repeat_penalty` (1.0 = off). Named `repetition_penalty` here for
    # the HF/vendor convention; the local client maps it onto llama.cpp's flag name.
    repetition_penalty: float | None = None

    def merge(self, other: "Sampling | None") -> "Sampling":
        """Overlay `other` on top of self: every field `other` sets (non-None) wins,
        the rest keep self's value. This is how a per-task `.prompt` override lands
        on top of the model's recommended defaults."""
        if other is None:
            return self
        # Explicit None check, NOT `or`: a 0 override (greedy temperature, min_p 0,
        # top_k 0 to disable it) is a real value that must win, not be treated as unset.
        return Sampling(
            **{
                f.name: o if (o := getattr(other, f.name)) is not None else getattr(self, f.name)
                for f in fields(self)
            }
        )

    @property
    def is_empty(self) -> bool:
        """True when nothing is set — nothing goes on the wire, provider default wins."""
        return all(getattr(self, f.name) is None for f in fields(self))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Sampling":
        """Build a Sampling from a `.prompt` `config: sampling:` mapping, validating
        strictly so a typo fails at prompt-load time, never mid-call. Unknown keys and
        non-numeric values raise ValueError (the loader re-raises as PromptError)."""
        allowed = {f.name for f in fields(cls)}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown sampling keys {sorted(unknown)}; allowed: {sorted(allowed)}")
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"sampling {key!r} must be a number, got {value!r}")
            values[key] = int(value) if key == "top_k" else float(value)
        return cls(**values)


@dataclass(frozen=True)
class LlmResult:
    """Adapter response: raw text always; `parsed` only when a JSON schema was
    requested and the text parsed (None signals the router to re-ask).

    `reasoning` is the model's thinking trace when the provider splits one onto a
    separate channel (a local reasoning model served with `--reasoning-format
    deepseek`, or gpt-oss/GLM harmony) — "" for a non-thinking model or a provider
    that folds thinking into the answer. Display/observability only: it is never the
    answer and never fed to grounding. Capturing it here keeps a thinking model's
    trace OUT of `text` for one-shot `complete()` callers the same way `converse`
    already separates it."""

    text: str
    parsed: Any | None
    usage: LlmUsage
    reasoning: str = ""


class UsageRecorder(Protocol):
    """Persists one call's token usage (docs/reference/ANALYSIS.md "Token accounting").

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
    it requested (empty unless `stop_reason == "tool_use"`), and usage.

    `reasoning` is the model's thinking trace when the provider emits one (gpt-oss /
    GLM via the local gateway's `reasoning_content`); "" for providers that don't.
    It is display/provenance only — never the answer, and never fed to grounding."""

    text: str
    tool_calls: Sequence[ToolCall]
    stop_reason: StopReason
    usage: LlmUsage
    reasoning: str = ""


@dataclass(frozen=True)
class TextChunk:
    """One incremental slice of streamed assistant text. The loop forwards these
    to the phone as `text_delta` events so the answer renders token-by-token."""

    text: str


@dataclass(frozen=True)
class ReasoningChunk:
    """One incremental slice of the model's streamed reasoning trace (the gpt-oss /
    GLM `reasoning_content` channel). The loop forwards these as `reasoning_delta`
    events so the "thinking" disclosure streams live; never part of the answer."""

    text: str


# A streamed turn is a sequence of incremental TextChunks (and, for a reasoning
# model, ReasoningChunks) followed by exactly one final LlmTurn: text/reasoning
# stream live, while tool calls are assembled whole (their arguments arrive as
# fragments and are only valid once complete) and carried on the closing turn
# alongside the full text, reasoning, stop reason, and usage.
StreamPart = TextChunk | ReasoningChunk | LlmTurn


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
        sampling: Sampling | None = None,
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
        sampling: Sampling | None = None,
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
        sampling: Sampling | None = None,
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
