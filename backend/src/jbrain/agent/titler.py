"""Auto-titling a Full Brain chat from its opening message (docs/reference/ASSISTANT.md
"Sessions").

A chat the owner didn't name gets a short, human title generated from its first
message — through the LLM adapter (CLAUDE.md rule 1), never a provider SDK — on the
SAME model the chat turn runs (passed as `spec_override`), so titling never swaps in
a different model or forces a cold reload. It runs as a quick turn BEFORE the main
response, so the tab is named up front. Best-effort: an empty/failed result leaves
the chat untitled (the UI shows a placeholder) and never breaks the turn.
"""

from pathlib import Path

from jbrain.llm import LlmRouter
from jbrain.llm.promptfile import load_prompt

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "session_title.prompt")
_SYSTEM = _PROMPT.render()
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

    async def title_for(
        self, *, question: str, answer: str = "", spec_override: str | None = None
    ) -> str:
        q = question.strip()
        if not q:
            return ""
        user_text = f"First message: {q}"
        if answer.strip():
            user_text += f"\n\nAssistant replied: {answer.strip()}"
        # No `strength`: with strength=None the router routes session.title to the SAME
        # model as agent.turn (its _FOLLOW_PRIMARY_MODEL path), and `spec_override` (the
        # per-conversation omnibox pick) outranks that — so the title always runs on the
        # chat's own model, already resident, rather than the low tier's separate model.
        result = await self._router.complete(
            "session.title",
            system=_SYSTEM,
            user_text=user_text,
            max_tokens=_MAX_TOKENS,
            spec_override=spec_override,
        )
        return _clean(result.text)
