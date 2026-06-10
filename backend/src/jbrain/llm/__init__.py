"""LLM adapter: the sole egress point for model calls (CLAUDE.md rule 1).

Application code uses LlmRouter via build_router; provider clients exist only
behind it. Tests use FakeLlmClient.
"""

from jbrain.llm.anthropic import AnthropicClient
from jbrain.llm.errors import (
    LlmAuthError,
    LlmBadResponseError,
    LlmError,
    LlmRateLimitError,
    LlmTransientError,
)
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.router import TASK_DEFAULTS, LlmRouter, build_router, resolve_tasks
from jbrain.llm.types import LlmClient, LlmImage, LlmResult, LlmUsage

__all__ = [
    "TASK_DEFAULTS",
    "AnthropicClient",
    "FakeLlmClient",
    "LlmAuthError",
    "LlmBadResponseError",
    "LlmClient",
    "LlmError",
    "LlmImage",
    "LlmRateLimitError",
    "LlmResult",
    "LlmRouter",
    "LlmTransientError",
    "LlmUsage",
    "OpenAiCompatClient",
    "build_router",
    "resolve_tasks",
]
