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


class LlmBadResponseError(LlmError):
    """The provider answered, but with something unusable: a non-retryable
    4xx, a malformed body, or JSON output that failed parsing even after
    the one re-ask."""
