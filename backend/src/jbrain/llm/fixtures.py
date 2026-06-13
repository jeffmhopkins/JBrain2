"""Content-addressed fixture LLM client — the dev/test backend (plan W0.3, §5).

The fake in `llm.fake` scripts turns inline for a single unit test. This client
solves the other half: replaying *authored* model responses against *real*
assembled prompts, deterministically, with no provider and no token.

How it earns its keep:
- **Replay (CI/tests):** a response is keyed by a hash of the exact request
  (model + system + prompt/messages + tools). Identical prompt → identical
  response, every run. This is the deterministic backend the convergence test
  and end-to-end loop tests run against.
- **Record (dev — "stand in for Grok"):** on a cache miss, the unseen request is
  dumped to `_pending/<key>.json` and a `MissingFixture` is raised carrying the
  full prompt. A human (or Claude, authoring Grok-stand-in output) writes the
  response; saved, it becomes a permanent replay fixture. Walking an agent loop
  this way records turn 1 (author the tool call), the loop runs the tool, the
  next `converse` is a new prompt → a new miss → author turn 2, and so on.

The seam is the `LlmClient` protocol, so swapping this for the real xAI client
later is a config change, not a code change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    StreamPart,
    TextChunk,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    parse_json_payload,
)

# Fixtures pin a synthetic token count — usage accounting is exercised by its own
# tests; replay only needs the shape, not real counts.
_FIXTURE_USAGE = LlmUsage(input_tokens=0, output_tokens=0)


class MissingFixture(LookupError):
    """No recorded response for this request. Carries the full prompt so a human
    can author one; in record mode the prompt is also written to `_pending/`."""

    def __init__(self, key: str, prompt: dict[str, Any]):
        self.key = key
        self.prompt = prompt
        super().__init__(f"no fixture for request {key} (prompt captured for authoring)")


# --- Canonical request keys -------------------------------------------------
#
# The key must be stable across runs but change when anything the model would
# condition on changes (system prompt, user text, tool definitions, message
# history). Images are keyed by a digest, not their base64 (stable, compact).


def _image_digest(images: Sequence[LlmImage]) -> list[str]:
    return [hashlib.sha256((i.media_type + i.data).encode()).hexdigest()[:16] for i in images]


def _message_repr(m: LlmMessage) -> dict[str, Any]:
    if isinstance(m, UserMessage):
        return {"role": "user", "text": m.text, "images": _image_digest(m.images)}
    if isinstance(m, AssistantMessage):
        return {
            "role": "assistant",
            "text": m.text,
            "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in m.tool_calls],
        }
    if isinstance(m, ToolResultMessage):
        return {
            "role": "tool_result",
            "results": [
                {"id": r.tool_call_id, "content": r.content, "is_error": r.is_error}
                for r in m.results
            ],
        }
    raise TypeError(f"unknown message type {type(m)!r}")  # pragma: no cover


def _tool_repr(tools: Sequence[LlmTool]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def _key(prompt: dict[str, Any]) -> str:
    canonical = json.dumps(prompt, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _complete_prompt(
    *, model: str, system: str, user_text: str, images: Sequence[LlmImage], with_schema: bool
) -> dict[str, Any]:
    return {
        "op": "complete",
        "model": model,
        "system": system,
        "user_text": user_text,
        "images": _image_digest(images),
        "with_schema": with_schema,
    }


def _converse_prompt(
    *, model: str, system: str, messages: Sequence[LlmMessage], tools: Sequence[LlmTool]
) -> dict[str, Any]:
    return {
        "op": "converse",
        "model": model,
        "system": system,
        "messages": [_message_repr(m) for m in messages],
        "tools": _tool_repr(tools),
    }


# --- (De)serialization of recorded turns ------------------------------------


def turn_to_dict(turn: LlmTurn) -> dict[str, Any]:
    return {
        "text": turn.text,
        "stop_reason": turn.stop_reason,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls
        ],
    }


def turn_from_dict(d: dict[str, Any]) -> LlmTurn:
    calls = tuple(
        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
        for c in d.get("tool_calls", ())
    )
    stop = d.get("stop_reason") or ("tool_use" if calls else "end_turn")
    return LlmTurn(text=d.get("text", ""), tool_calls=calls, stop_reason=stop, usage=_FIXTURE_USAGE)


class FixtureLlmClient:
    """Replays recorded responses keyed by request content; records misses."""

    def __init__(self, directory: Path | str, *, record: bool = False):
        self._dir = Path(directory)
        self._record = record
        self.calls: list[dict[str, Any]] = []  # every request seen (assertable)

    # -- storage --
    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _capture_pending(self, key: str, prompt: dict[str, Any]) -> None:
        pending = self._dir / "_pending"
        pending.mkdir(parents=True, exist_ok=True)
        # response: null is the slot a human fills, then moves the file up to
        # <dir>/<key>.json. The prompt is human-readable for authoring.
        (pending / f"{key}.json").write_text(
            json.dumps({"key": key, "prompt": prompt, "response": None}, indent=2)
        )

    def _resolve(self, key: str, prompt: dict[str, Any]) -> dict[str, Any]:
        fixture = self._load(key)
        if fixture is None:
            if self._record:
                self._capture_pending(key, prompt)
            raise MissingFixture(key, prompt)
        return fixture

    # -- authoring helpers (used by dev/tests to write fixtures) --
    def write_complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        text: str,
        images: Sequence[LlmImage] = (),
        with_schema: bool = False,
    ) -> str:
        # with_schema must match the eventual call: note.extract is always called
        # WITH a json_schema, so its fixtures are authored with_schema=True.
        prompt = _complete_prompt(
            model=model, system=system, user_text=user_text, images=images, with_schema=with_schema
        )
        return self._write(prompt, {"text": text})

    def write_converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        turn: LlmTurn,
        tools: Sequence[LlmTool] = (),
    ) -> str:
        prompt = _converse_prompt(model=model, system=system, messages=messages, tools=tools)
        return self._write(prompt, {"turn": turn_to_dict(turn)})

    def _write(self, prompt: dict[str, Any], response: dict[str, Any]) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        key = _key(prompt)
        self._path(key).write_text(json.dumps({"prompt": prompt, **response}, indent=2))
        return key

    # -- the LlmClient protocol --
    async def complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmResult:
        prompt = _complete_prompt(
            model=model,
            system=system,
            user_text=user_text,
            images=images,
            with_schema=json_schema is not None,
        )
        key = _key(prompt)
        self.calls.append(prompt)
        fixture = self._resolve(key, prompt)
        text = fixture["text"]
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=_FIXTURE_USAGE)

    async def converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmTurn:
        prompt = _converse_prompt(model=model, system=system, messages=messages, tools=tools)
        key = _key(prompt)
        self.calls.append(prompt)
        fixture = self._resolve(key, prompt)
        return turn_from_dict(fixture["turn"])

    async def converse_stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> AsyncIterator[StreamPart]:
        turn = await self.converse(
            model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        if turn.text:
            yield TextChunk(text=turn.text)
        yield turn
