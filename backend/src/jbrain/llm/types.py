"""Provider-agnostic request/response types and the LlmClient protocol."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

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
    ) -> LlmResult: ...


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
