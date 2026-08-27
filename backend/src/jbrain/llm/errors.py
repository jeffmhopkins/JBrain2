"""Error taxonomy for the LLM adapter.

Callers branch on these instead of provider-specific status codes: auth
failures are config bugs (never retried), rate limits and transient faults
are retried here and then surfaced for queue-level backoff, and bad-response
means the provider answered but the answer is unusable.
"""


class LlmError(Exception):
    """Base for every adapter failure."""


class LlmAuthError(LlmError):
    """Invalid or missing credentials (401/403) — retrying cannot help."""


class LlmRateLimitError(LlmError):
    """Provider rate limit (429) persisted through the adapter's retries."""


class LlmTransientError(LlmError):
    """Network failure or 5xx that persisted through the adapter's retries."""


class LlmStreamTruncatedError(LlmTransientError):
    """An SSE stream ended before the provider ever sent a `finish_reason`: the turn was
    cut off mid-generation.

    Distinct from a network drop — the body ended at a clean event boundary, just early —
    which is exactly what makes it dangerous. The deltas that did arrive look like a
    complete turn, so without this the adapter yields a perfectly well-formed `LlmTurn`
    carrying whatever fragment made it through, `stop_reason="end_turn"` and zero usage,
    and every caller reads that as "the model chose to say nothing". Observed live on
    2026-08-27: gpt-oss-120b streamed its reasoning, the `tool_calls` deltas and the
    finish/usage chunks never arrived, and nine of one agent's sixteen turns were recorded
    as successful empty turns instead of the tool calls they actually were.

    A subclass of LlmTransientError because the round IS retryable — the same call
    non-streaming succeeded 12/12 against the same model — so callers that can re-issue
    should, and everyone else gets the existing transient handling instead of silence."""


class LlmBadResponseError(LlmError):
    """The provider answered, but with something unusable: a non-retryable
    4xx, a malformed body, or JSON output that failed parsing even after
    the one re-ask."""


class LlmContextOverflowError(LlmBadResponseError):
    """The request exceeded the model's context window — the gateway rejected it
    (llama.cpp answers 400 "the request exceeds the available context size"). A
    subclass of LlmBadResponseError so it keeps the same non-retryable handling and
    any existing `except LlmBadResponseError`, but callers that want to tell the owner
    "this model ran out of context" (vs a generic failure) can catch it specifically.
    Carries NO body text — the classification reads the response, the error does not
    propagate it (prompts/answers are private, per retry.py's body-free-logs rule)."""
