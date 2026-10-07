"""OpenAI-compatible chat-completions client over raw httpx.

Serves two providers: xAI (https://api.x.ai/v1) and the local escape hatch
(Ollama-style server at JBRAIN_LOCAL_LLM_URL). Keeping them on one client is
what makes "go all-local" a config flip instead of a refactor — see
docs/reference/ANALYSIS.md "Privacy routing".
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, cast

import httpx
import structlog

from jbrain.llm import local_catalog
from jbrain.llm.errors import (
    LlmBadResponseError,
    LlmStreamTruncatedError,
    LlmVideoUnsupportedError,
)
from jbrain.llm.retry import post_json, stream_sse
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    LlmVideo,
    ReasoningChunk,
    Sampling,
    StopReason,
    StreamPart,
    TextChunk,
    ToolCall,
    UserMessage,
    parse_json_payload,
    replayed_steps,
)

log = structlog.get_logger()


def _timings_cached(body: Any) -> int:
    """llama-server's own `timings.cache_n` (prompt tokens reused from the slot), for a build
    whose usage block carries no `cached_tokens`; 0 when absent or malformed."""
    timings = body.get("timings") if isinstance(body, dict) else None
    value = timings.get("cache_n") if isinstance(timings, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


def _cached_tokens(usage_body: Any) -> int:
    """`prompt_tokens_details.cached_tokens`, 0 when absent or malformed."""
    details = usage_body.get("prompt_tokens_details") if isinstance(usage_body, dict) else None
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


DEFAULT_TIMEOUT = 120.0

# OpenAI finish_reason → our normalized stop reason.
_OPENAI_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


# What a capped thought ends on, before the forced think-end tag.
REASONING_BUDGET_MESSAGE = "\n\nThat is enough thinking; I will act on it now.\n"


def openai_tools(tools: Sequence[LlmTool]) -> list[dict[str, Any]]:
    """Serialize adapter tools into the OpenAI `tools` array. The single source of
    this shape so a gateway warm-up (jbrain.agent.priming) primes the SAME tool JSON a
    real turn sends — under `--jinja` the template renders these into the prompt's
    leading tokens, so any drift breaks the `--cache-reuse` prefix match."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def apply_local_reasoning(payload: dict[str, Any], reasoning_effort: str | None) -> None:
    """Put a LOCAL model's reasoning setting on the wire, translated per family — the one
    encoding, shared by real turns and the gateway's load-time warm-up. Under `--jinja`
    the chat template renders this into the prompt's LEADING tokens (gpt-oss's harmony
    template writes a literal "Reasoning: <level>" header), so a warm that encodes it
    differently from the turn it warms for shares no cache prefix at all — observed live
    2026-08-23 as a full ~62 s prefill on every load, right past a 497 ms KV restore.

      - A Qwen HYBRID reasoner toggles thinking through its chat template, NOT a
        top-level `reasoning_effort` (which its template ignores): "none" →
        enable_thinking=false, any other level → thinking on.
      - A NEWER hybrid (Qwen3.8 onward) also reads a mapped `reasoning_effort` level from
        the same chat-template kwargs bag (its own default is `xhigh`, which burns
        thousands of reasoning tokens on trivial prompts).
      - The harmony/GLM reasoners take the effort level verbatim — EXCEPT that gpt-oss's
        harmony template has no genuine "none": llama.cpp maps `reasoning_effort:"none"` to
        `enable_thinking=false` + an erased effort, but the harmony template ignores the flag
        and defaults the erased effort to "medium", so "none" silently runs at MEDIUM. A model
        flagged `no_reasoning_off` therefore sends "none" as "low" (its real floor) instead.

    `payload["model"]` must be the served name; a None effort is a no-op (non-reasoning
    models must not carry the field — their templates would still render it)."""
    if reasoning_effort is None:
        return
    model = local_catalog.get_by_served(str(payload["model"]))
    if model is not None and model.hybrid_thinking:
        kwargs = cast(dict[str, Any], payload.setdefault("chat_template_kwargs", {}))
        kwargs["enable_thinking"] = reasoning_effort != "none"
        mapped = model.thinking_effort_map.get(reasoning_effort)
        if mapped:
            kwargs["reasoning_effort"] = mapped
        return
    if reasoning_effort == "none" and model is not None and model.no_reasoning_off:
        reasoning_effort = "low"
    payload["reasoning_effort"] = reasoning_effort


def _user_content(
    text: str, images: Sequence[LlmImage], videos: Sequence[LlmVideo] = ()
) -> str | list[dict[str, Any]]:
    if not images and not videos:
        return text
    parts: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": f"data:{i.media_type};base64,{i.data}"}}
        for i in images
    ]
    parts.extend({"type": "input_video", "input_video": {"data": v.data}} for v in videos)
    parts.append({"type": "text", "text": text})
    return parts


def _openai_messages(
    system: str, messages: Sequence[LlmMessage], *, replay_model: str = ""
) -> list[dict[str, Any]]:
    """Flatten provider-agnostic messages into the OpenAI chat array. Tool
    results become individual `tool`-role messages, one per result.

    `replay_model` (a preserving local model the router chose to replay to) puts each of its
    own steps' traces back as `reasoning_content` — earlier turns' too (`replayed_steps`), so
    the next turn's prompt extends this one byte for byte. The growth that costs is bounded
    upstream, by the transcript replay's budget (`agent/history_replay.py`)."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    replayed = replayed_steps(messages, replay_model)
    for index, msg in enumerate(messages):
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": _user_content(msg.text, msg.images, msg.videos)})
        elif isinstance(msg, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant", "content": msg.text or None}
            if index in replayed:
                entry["reasoning_content"] = msg.reasoning
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in msg.tool_calls
                ]
            out.append(entry)
        else:  # ToolResultMessage
            out.extend(
                {"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
                for r in msg.results
            )
    return out


class OpenAiCompatClient:
    """POST {base_url}/chat/completions with Bearer auth and image_url parts."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        provider: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.provider = provider
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        videos: Sequence[LlmVideo] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
        sampling: Sampling | None = None,
        id_slot: int | None = None,
    ) -> LlmResult:
        # Only llama.cpp's server decodes `input_video`; xAI would 4xx it after the upload.
        if videos and self.provider != local_catalog.LOCAL_PROVIDER:
            raise LlmVideoUnsupportedError(f"{self.provider}: video input is not supported")
        user_content: str | list[dict[str, Any]]
        if images or videos:
            user_content = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{img.media_type};base64,{img.data}"},
                }
                for img in images
            ]
            # Raw base64, no data: URI — llama.cpp's server reads `input_video.data` as the
            # clip's bytes and sniffs the container itself.
            user_content.extend(
                {"type": "input_video", "input_video": {"data": v.data}} for v in videos
            )
            user_content.append({"type": "text", "text": user_text})
        else:
            user_content = user_text
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        self._apply_reasoning(payload, reasoning_effort)
        self._apply_sampling(payload, sampling)
        self._apply_slot(payload, id_slot)
        # Local servers run keyless; omitting the header beats sending "Bearer ".
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = await post_json(
            f"{self._base_url}/chat/completions",
            headers=headers,
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        try:
            message = data["choices"][0]["message"]
            text = message.get("content") or ""
            # A reasoning model served with `--reasoning-format deepseek` splits its
            # `<think>` trace onto this channel; capture it so a one-shot's thinking
            # stays OUT of `text` (mirroring `converse`) instead of being an invisible
            # empty answer when the trace ran but no visible content followed.
            reasoning = message.get("reasoning_content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmBadResponseError(f"{self.provider}: unexpected response shape") from exc
        # Local servers may omit usage; zeros keep the call observable anyway.
        usage_body = data.get("usage") or {}
        usage = LlmUsage(
            input_tokens=int(usage_body.get("prompt_tokens", 0)),
            output_tokens=int(usage_body.get("completion_tokens", 0)),
            cached_tokens=_cached_tokens(usage_body),
        )
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=usage, reasoning=reasoning)

    def _apply_reasoning(self, payload: dict[str, Any], reasoning_effort: str | None) -> None:
        # Put the routed reasoning setting on the wire. The ROUTER gates eligibility
        # (it only sets a level for a reasoning-capable provider+model), so a
        # non-reasoning local model never reaches here with a level set. xAI Grok takes
        # the effort verbatim; the local families each have their own wire shape — see
        # `apply_local_reasoning`, module-level because the gateway's load-time warm-up
        # must render EXACTLY this too (the effort lands in the prompt's leading tokens,
        # so a warm that omits it shares no cache with the turns it claims to warm).
        if reasoning_effort is None or self.provider not in ("xai", "local"):
            return
        if self.provider == "local":
            apply_local_reasoning(payload, reasoning_effort)
            return
        payload["reasoning_effort"] = reasoning_effort

    def _apply_sampling(self, payload: dict[str, Any], sampling: Sampling | None) -> None:
        # Put the resolved sampling on the wire, honoring each provider's param support.
        # temperature/top_p are OpenAI-standard, so both providers take them. The rest are
        # LOCAL-only: xAI's OpenAI-compatible API doesn't accept top_k/min_p, and its Grok 4.x
        # reasoning models REJECT presence/frequency penalties (a 4xx). So a task override that
        # sets a penalty (the vision.ocr near-greedy one, meant for the local VL model) lands
        # correctly on local and is simply omitted for cloud Grok — no error, no config fork.
        # llama.cpp names the repetition knob `repeat_penalty`; we map onto that here.
        if sampling is None or sampling.is_empty:
            return
        if sampling.temperature is not None:
            payload["temperature"] = sampling.temperature
        if sampling.top_p is not None:
            payload["top_p"] = sampling.top_p
        if self.provider != "local":
            return
        if sampling.top_k is not None:
            payload["top_k"] = sampling.top_k
        if sampling.min_p is not None:
            payload["min_p"] = sampling.min_p
        if sampling.presence_penalty is not None:
            payload["presence_penalty"] = sampling.presence_penalty
        if sampling.frequency_penalty is not None:
            payload["frequency_penalty"] = sampling.frequency_penalty
        if sampling.repetition_penalty is not None:
            payload["repeat_penalty"] = sampling.repetition_penalty
        if sampling.reasoning_budget is not None:
            # llama-server's per-request fields (server-common.cpp, pin 869034b). The message
            # is spliced in before the forced think-end tag, so the cut reads as a decision
            # to act rather than a sentence chopped off mid-thought.
            payload["reasoning_budget_tokens"] = sampling.reasoning_budget
            payload["reasoning_budget_message"] = REASONING_BUDGET_MESSAGE

    def _converse_payload(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool],
        max_tokens: int,
        reasoning_effort: str | None = None,
        sampling: Sampling | None = None,
        id_slot: int | None = None,
        replay_reasoning: bool = False,
    ) -> dict[str, Any]:
        # The router decides whether to replay (it may turn it off to fit a slot); the catalog
        # gate is re-checked here so no caller can put thinking on a wire that cannot take it.
        preserving = local_catalog.replays_reasoning(self.provider, model)
        if self.provider != local_catalog.LOCAL_PROVIDER and any(
            isinstance(m, UserMessage) and m.videos for m in messages
        ):
            raise LlmVideoUnsupportedError(f"{self.provider}: video input is not supported")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _openai_messages(
                system, messages, replay_model=model if replay_reasoning and preserving else ""
            ),
        }
        if tools:
            payload["tools"] = openai_tools(tools)
        self._apply_reasoning(payload, reasoning_effort)
        if preserving:
            # Every historical assistant step renders with its thinking, the line the replay
            # above draws. `false` would render a step WITH its trace while its turn runs and
            # WITHOUT it once a newer user message arrives — the follow-up's prompt then
            # diverges at the previous turn's first step, and a hybrid model, which reuses only
            # from a context checkpoint before the divergence, re-read a 117k-token chat
            # (measured 2026-10-07). Explicit, not the template default, so a template update
            # cannot move it; sent on every call to this model, replayed or not.
            kwargs = cast(dict[str, Any], payload.setdefault("chat_template_kwargs", {}))
            kwargs["preserve_thinking"] = True
        self._apply_sampling(payload, sampling)
        self._apply_slot(payload, id_slot)
        return payload

    def _apply_slot(self, payload: dict[str, Any], id_slot: int | None) -> None:
        # llama-server's own field; a cloud OpenAI-compatible API would reject or misread it.
        if id_slot is not None and self.provider == "local":
            payload["id_slot"] = id_slot

    def _auth_headers(self) -> dict[str, str]:
        # Local servers run keyless; omitting the header beats sending "Bearer ".
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

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
        id_slot: int | None = None,
        replay_reasoning: bool = False,
    ) -> LlmTurn:
        payload = self._converse_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            sampling=sampling,
            id_slot=id_slot,
            replay_reasoning=replay_reasoning,
        )
        headers = self._auth_headers()
        data = await post_json(
            f"{self._base_url}/chat/completions",
            headers=headers,
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            reasoning = message.get("reasoning_content") or ""
            tool_calls = tuple(self._tool_call(tc) for tc in message.get("tool_calls") or ())
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmBadResponseError(f"{self.provider}: unexpected response shape") from exc
        usage_body = data.get("usage") or {}
        usage = LlmUsage(
            input_tokens=int(usage_body.get("prompt_tokens", 0)),
            output_tokens=int(usage_body.get("completion_tokens", 0)),
            cached_tokens=_cached_tokens(usage_body) or _timings_cached(data),
        )
        return LlmTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=_OPENAI_STOP.get(finish, "end_turn"),
            usage=usage,
            reasoning=reasoning,
        )

    def _tool_call(self, raw: dict[str, Any]) -> ToolCall:
        """Parse one OpenAI tool_call; its arguments are a JSON *string*."""
        fn = raw["function"]
        arguments = parse_json_payload(fn.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise LlmBadResponseError(
                f"{self.provider}: tool_call arguments were not a JSON object"
            )
        return ToolCall(id=raw["id"], name=fn["name"], arguments=arguments)

    async def converse_stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
        sampling: Sampling | None = None,
        id_slot: int | None = None,
        replay_reasoning: bool = False,
    ) -> AsyncIterator[StreamPart]:
        """Stream a turn over chat-completions SSE chunks. Content deltas stream
        live; tool_call deltas arrive fragmented and keyed by index (id/name on
        the first, argument fragments after), assembled whole at the end.
        `include_usage` asks for a trailing usage-only chunk (local servers may
        omit it; usage then stays zero, as in `converse`)."""
        payload = self._converse_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            sampling=sampling,
            id_slot=id_slot,
            replay_reasoning=replay_reasoning,
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        events = stream_sse(
            f"{self._base_url}/chat/completions",
            headers=self._auth_headers(),
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Tool calls keyed by their stream index: id/name land on the first delta
        # for that index, argument fragments accumulate across later ones.
        calls_by_index: dict[int, dict[str, Any]] = {}
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        stop: StopReason = "end_turn"
        # Whether the provider ever told us the turn was over. A stream that ends without
        # it was cut mid-generation, and the difference is invisible in the deltas — see
        # the raise below.
        saw_finish = False
        async for event in events:
            usage_body = event.get("usage")
            if usage_body:
                input_tokens = int(usage_body.get("prompt_tokens", input_tokens))
                output_tokens = int(usage_body.get("completion_tokens", output_tokens))
                cached_tokens = _cached_tokens(usage_body) or cached_tokens
            cached_tokens = cached_tokens or _timings_cached(event)
            for choice in event.get("choices") or ():
                delta = choice.get("delta") or {}
                # A reasoning model (gpt-oss/GLM via the local gateway) streams its
                # thinking on a separate `reasoning_content` channel; surface it as a
                # ReasoningChunk — never folded into the answer text.
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield ReasoningChunk(text=reasoning)
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    yield TextChunk(text=content)
                for tc in delta.get("tool_calls") or ():
                    self._accumulate_tool_call(calls_by_index, tc)
                finish = choice.get("finish_reason")
                if finish:
                    saw_finish = True
                    stop = _OPENAI_STOP.get(finish, "end_turn")
        if not saw_finish:
            # The body ended cleanly but early: no finish_reason ever arrived, so the turn
            # was cut mid-generation and whatever we accumulated is a fragment. Yielding it
            # would be indistinguishable from a real turn — `stop` still reads "end_turn"
            # from its default and usage is zero — which is precisely how this went unnoticed
            # in production: an agent's tool call was dropped on the wire and the run was
            # recorded as a successful empty turn. Refuse rather than hand back a fragment
            # wearing a completed turn's clothes. Body-free log, per retry.py's rule; the
            # SHAPE of what survived is the diagnostic, never its text.
            log.warning(
                "llm.stream_truncated",
                provider=self.provider,
                model=model,
                reasoning_chars=sum(len(p) for p in reasoning_parts),
                text_chars=sum(len(p) for p in text_parts),
                tool_calls=len(calls_by_index),
            )
            raise LlmStreamTruncatedError(
                f"{self.provider} stream ended without a finish_reason "
                f"({len(calls_by_index)} partial tool call(s) discarded)"
            )
        tool_calls = tuple(self._finish_tool_call(buf) for _, buf in sorted(calls_by_index.items()))
        yield LlmTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=LlmUsage(
                input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens
            ),
            reasoning="".join(reasoning_parts),
        )

    @staticmethod
    def _accumulate_tool_call(buffers: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
        buf = buffers.setdefault(int(delta.get("index", 0)), {"id": "", "name": "", "args": ""})
        if delta.get("id"):
            buf["id"] = delta["id"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            buf["name"] = fn["name"]
        buf["args"] += fn.get("arguments") or ""

    def _finish_tool_call(self, buf: dict[str, Any]) -> ToolCall:
        arguments = parse_json_payload(buf["args"] or "{}")
        if not isinstance(arguments, dict):
            raise LlmBadResponseError(
                f"{self.provider}: streamed tool_call arguments were not a JSON object"
            )
        return ToolCall(id=buf["id"], name=buf["name"], arguments=arguments)
