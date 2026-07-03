"""Auto-titling a Full Brain chat from its first exchange (docs/reference/ASSISTANT.md
"Sessions").

A chat the owner didn't name gets a short, human title generated from its first
question + answer — through the LLM adapter (CLAUDE.md rule 1), never a provider
SDK. Titling is best-effort: an empty/failed result leaves the chat untitled (the
UI shows a placeholder) and never breaks the turn that produced it.
"""

from pathlib import Path

from jbrain.llm import LlmRouter
from jbrain.llm.promptfile import load_prompt

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "session_title.prompt")
_SYSTEM = _PROMPT.render()
_STRENGTH = _PROMPT.strength
_MAX_TOKENS = int(_PROMPT.config["max_tokens"])
_MAX_LEN = 60


def _clean(raw: str) -> str:
    """First line only, stripped of surrounding quotes and trailing punctuation,
    capped — a model that adds a flourish still yields a tidy label."""
    head = next((line for line in raw.splitlines() if line.strip()), "")
    return head.strip().strip("\"'“”").rstrip(".").strip()[:_MAX_LEN].strip()


class SessionTitler:
    """Generates a chat title from its opening exchange."""

    def __init__(self, router: LlmRouter):
        self._router = router

    async def title_for(self, *, question: str, answer: str) -> str:
        q = question.strip()
        if not q:
            return ""
        user_text = f"First message: {q}"
        if answer.strip():
            user_text += f"\n\nAssistant replied: {answer.strip()}"
        result = await self._router.complete(
            "session.title",
            system=_SYSTEM,
            user_text=user_text,
            max_tokens=_MAX_TOKENS,
            strength=_STRENGTH,
        )
        return _clean(result.text)
