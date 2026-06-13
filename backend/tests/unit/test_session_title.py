"""The session auto-titler (no DB): the LLM call goes through the router/adapter
and the raw reply is cleaned into a tidy one-line label."""

import pytest

from jbrain.agent.titler import SessionTitler, _clean
from jbrain.llm import FakeLlmClient, LlmRouter


def _router(text: str) -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(responses=[text])
    return LlmRouter({"xai": fake}, {"session.title": ("xai", "grok-4.3")}), fake


async def test_titles_from_the_first_exchange() -> None:
    router, fake = _router("Roof Quote Second Opinion")
    title = await SessionTitler(router).title_for(
        question="should I get a second roof quote?", answer="Yes — compare to last year."
    )
    assert title == "Roof Quote Second Opinion"
    # Both the question and the answer reached the model.
    assert "should I get a second roof quote?" in fake.calls[0]["user_text"]
    assert "compare to last year" in fake.calls[0]["user_text"]


async def test_blank_question_skips_the_model() -> None:
    router, fake = _router("X")
    assert await SessionTitler(router).title_for(question="   ", answer="a") == ""
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
