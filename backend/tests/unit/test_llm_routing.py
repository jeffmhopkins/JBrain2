"""Task-profile routing: defaults, override merging, and config parsing."""

import pytest

from jbrain.config import Settings
from jbrain.llm import LlmError, resolve_tasks
from jbrain.llm.router import TASK_DEFAULTS

# `note.extract` and `integrate.note` were here until R4 deleted their prompts with the
# producer that called them; a routed task nothing calls is a lever in the owner's
# Settings screen that does nothing.
EXPECTED_TASKS = {
    "entity.disambiguate",
    "fact.adjudicate",
    "correction_note.extract",
    "vision.ocr",
    "vision.caption",
    "agent.turn",
    "agent.vision",
    "video.summarize",
    "research.title",
    "intake.materialize",
    "wiki.rewrite",
    "wiki.ground",
    "wiki.lint.contradiction",
    "wiki.lint.stale",
    "triage.classify",
    "pet.turn",
    "pet.thought",
    "pet.statue",
}


def test_every_task_defaults_to_xai_grok() -> None:
    # OWNER DECISION: the default for EVERY task is "xai:grok-4.3".
    assert set(TASK_DEFAULTS) == EXPECTED_TASKS
    assert resolve_tasks({}) == {task: ("xai", "grok-4.3") for task in EXPECTED_TASKS}


def test_override_replaces_only_named_task() -> None:
    tasks = resolve_tasks({"correction_note.extract": "anthropic:claude-sonnet-4-6"})
    assert tasks["correction_note.extract"] == ("anthropic", "claude-sonnet-4-6")
    assert tasks["fact.adjudicate"] == ("xai", "grok-4.3")


def test_local_provider_is_routable() -> None:
    tasks = resolve_tasks({"vision.ocr": "local:llava"})
    assert tasks["vision.ocr"] == ("local", "llava")


def test_unknown_task_in_overrides_raises() -> None:
    with pytest.raises(LlmError, match="unknown LLM task"):
        resolve_tasks({"note.extrct": "xai:grok-4.3"})


def test_unknown_provider_raises() -> None:
    with pytest.raises(LlmError, match="unknown LLM provider"):
        resolve_tasks({"correction_note.extract": "openai:gpt-4o"})


def test_malformed_spec_raises() -> None:
    with pytest.raises(LlmError, match="malformed"):
        resolve_tasks({"correction_note.extract": "grok-4.3"})
    with pytest.raises(LlmError, match="malformed"):
        resolve_tasks({"correction_note.extract": "xai:"})


def test_settings_parse_llm_tasks_env_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "JBRAIN_LLM_TASKS", '{"correction_note.extract": "anthropic:claude-sonnet-4-6"}'
    )
    settings = Settings()
    assert settings.llm_tasks == {"correction_note.extract": "anthropic:claude-sonnet-4-6"}
    assert resolve_tasks(settings.llm_tasks)["correction_note.extract"] == (
        "anthropic",
        "claude-sonnet-4-6",
    )


def test_settings_default_local_llm_url() -> None:
    assert Settings().local_llm_url == "http://localhost:11434/v1"
