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
from jbrain.llm.types import (
    AssistantMessage,
    LlmClient,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    StopReason,
    StreamPart,
    TextChunk,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UsageRecorder,
    UserMessage,
)

__all__ = [
    "TASK_DEFAULTS",
    "AnthropicClient",
    "AssistantMessage",
    "FakeLlmClient",
    "LlmAuthError",
    "LlmBadResponseError",
    "LlmClient",
    "LlmError",
    "LlmImage",
    "LlmMessage",
    "LlmRateLimitError",
    "LlmResult",
    "LlmRouter",
    "LlmTool",
    "LlmTransientError",
    "LlmTurn",
    "LlmUsage",
    "OpenAiCompatClient",
    "StopReason",
    "StreamPart",
    "TextChunk",
    "ToolCall",
    "ToolResult",
    "ToolResultMessage",
    "UsageRecorder",
    "UserMessage",
    "build_router",
    "resolve_tasks",
]
