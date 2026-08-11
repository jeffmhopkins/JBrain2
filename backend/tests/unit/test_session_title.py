"""The session auto-titler (no DB): the LLM call goes through the router/adapter
and the raw reply is cleaned into a tidy one-line label."""

import pytest

from jbrain.agent.titler import SessionTitler, _clean
from jbrain.llm import FakeLlmClient, LlmRouter


def _router(text: str) -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(responses=[text])
    return LlmRouter({"xai": fake}, {"session.title": ("xai", "grok-4.3")}), fake


async def test_titles_from_the_opening_message() -> None:
    # The title now runs UP FRONT from the opening message alone (no answer yet).
    router, fake = _router("Roof Quote Second Opinion")
    title = await SessionTitler(router).title_for(question="should I get a second roof quote?")
    assert title == "Roof Quote Second Opinion"
    assert "should I get a second roof quote?" in fake.calls[0]["user_text"]


async def test_spec_override_runs_the_title_on_the_turns_own_model() -> None:
    # The whole point: the title runs on the SAME model the chat turn uses (passed as
    # spec_override, the omnibox pick), so naming a chat never swaps in a different model
    # or forces a cold reload. The override must land on the completion's model.
    router, fake = _router("A Title")
    await SessionTitler(router).title_for(question="hi", spec_override="xai:grok-turbo")
    assert fake.calls[0]["model"] == "grok-turbo"


async def test_budget_leaves_reasoning_headroom() -> None:
    # The chat model may be a reasoning model whose thinking trace is billed against
    # max_tokens before any visible answer. A budget sized only for the few-word title
    # was spent entirely on reasoning, returning empty content and leaving the chat
    # untitled (regression). A local tiny hybrid's trace ran ~2.3k tokens for one title,
    # so the budget must cover a full trace plus the label — guard that floor.
    router, fake = _router("A Title")
    await SessionTitler(router).title_for(question="hi")
    assert fake.calls[0]["max_tokens"] >= 2048


async def test_blank_question_skips_the_model() -> None:
    router, fake = _router("X")
    assert await SessionTitler(router).title_for(question="   ") == ""
    assert fake.calls == []  # no spend on an empty turn


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ('"Quoted Title"', "Quoted Title"),
        ("Title Here.\nstray second line", "Title Here"),
        ("  Spaced Out  ", "Spaced Out"),
        ("x" * 80, "x" * 60),  # capped
        ("\n\n", ""),
    ],
)
def test_clean_normalizes_the_reply(raw: str, cleaned: str) -> None:
    assert _clean(raw) == cleaned
