"""Task-profile routing: defaults, override merging, and config parsing."""

import pytest

from jbrain.config import Settings
from jbrain.llm import LlmError, resolve_tasks
from jbrain.llm.router import TASK_DEFAULTS

EXPECTED_TASKS = {
    "note.extract",
    "entity.disambiguate",
    "fact.adjudicate",
    "correction_note.extract",
    "vision.ocr",
    "vision.caption",
    "agent.turn",
    "agent.vision",
    "video.summarize",
    "session.title",
    "integrate.note",
    "intake.materialize",
    "wiki.rewrite",
    "wiki.ground",
    "wiki.lint.contradiction",
    "wiki.lint.stale",
    "triage.classify",
}


def test_every_task_defaults_to_xai_grok() -> None:
    # OWNER DECISION: the default for EVERY task is "xai:grok-4.3".
    assert set(TASK_DEFAULTS) == EXPECTED_TASKS
    assert resolve_tasks({}) == {task: ("xai", "grok-4.3") for task in EXPECTED_TASKS}


def test_override_replaces_only_named_task() -> None:
    tasks = resolve_tasks({"note.extract": "anthropic:claude-sonnet-4-6"})
    assert tasks["note.extract"] == ("anthropic", "claude-sonnet-4-6")
    assert tasks["fact.adjudicate"] == ("xai", "grok-4.3")


def test_local_provider_is_routable() -> None:
    tasks = resolve_tasks({"vision.ocr": "local:llava"})
    assert tasks["vision.ocr"] == ("local", "llava")


def test_unknown_task_in_overrides_raises() -> None:
    with pytest.raises(LlmError, match="unknown LLM task"):
        resolve_tasks({"note.extrct": "xai:grok-4.3"})


def test_unknown_provider_raises() -> None:
    with pytest.raises(LlmError, match="unknown LLM provider"):
        resolve_tasks({"note.extract": "openai:gpt-4o"})


def test_malformed_spec_raises() -> None:
    with pytest.raises(LlmError, match="malformed"):
        resolve_tasks({"note.extract": "grok-4.3"})
    with pytest.raises(LlmError, match="malformed"):
        resolve_tasks({"note.extract": "xai:"})


def test_settings_parse_llm_tasks_env_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JBRAIN_LLM_TASKS", '{"note.extract": "anthropic:claude-sonnet-4-6"}')
    settings = Settings()
    assert settings.llm_tasks == {"note.extract": "anthropic:claude-sonnet-4-6"}
    assert resolve_tasks(settings.llm_tasks)["note.extract"] == (
        "anthropic",
        "claude-sonnet-4-6",
    )


def test_settings_default_local_llm_url() -> None:
    assert Settings().local_llm_url == "http://localhost:11434/v1"
