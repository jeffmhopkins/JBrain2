"""The .prompt loader: frontmatter parsing, strict rendering, and fail-fast validation.
The real shipped files round-trip to the constants their modules import.

The note.extract round trip and its content/version digest went with the prompt in R4.
The equivalent guard for the producer that reads notes now is `test_agents.py`'s pinned
(version, prose digest) per persona — `note_ingest` among them — which is what keeps
`facts.prompt_version` honest."""

from pathlib import Path

import pytest

from jbrain.llm.promptfile import PromptError, load_prompt

_MINIMAL = """\
---
name: t.test
version: t-v1
strength: low
input: [who]
config: { max_tokens: 16 }
output:
  format: json
  schema: { type: object }
---
Hello {{ who }} — keep this literal: {"k": 1}.
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "x.prompt"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_renders_and_leaves_literal_braces(tmp_path: Path) -> None:
    pf = load_prompt(_write(tmp_path, _MINIMAL))
    assert pf.name == "t.test" and pf.version == "t-v1" and pf.strength == "low"
    assert pf.config["max_tokens"] == 16 and pf.output_schema == {"type": "object"}
    # The {{ who }} token is substituted; the JSON brace is left untouched.
    assert pf.render(who="world") == 'Hello world — keep this literal: {"k": 1}.'


def test_no_sampling_block_leaves_sampling_none(tmp_path: Path) -> None:
    pf = load_prompt(_write(tmp_path, _MINIMAL))
    assert pf.sampling is None  # runs at the resolved model's recommended defaults


def test_sampling_block_parses_into_a_validated_bundle(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        "config: { max_tokens: 16 }",
        "config: { max_tokens: 16, sampling: { temperature: 0.1, presence_penalty: 1.5 } }",
    )
    pf = load_prompt(_write(tmp_path, text))
    assert pf.sampling is not None
    assert pf.sampling.temperature == 0.1 and pf.sampling.presence_penalty == 1.5
    # The sampling sub-key stays in the raw config too (it is one config block).
    assert pf.config["max_tokens"] == 16


def test_bad_sampling_key_fails_at_load(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        "config: { max_tokens: 16 }",
        "config: { max_tokens: 16, sampling: { temperatur: 0.1 } }",
    )
    with pytest.raises(PromptError, match="unknown sampling keys"):
        load_prompt(_write(tmp_path, text))


def test_missing_template_var_raises(tmp_path: Path) -> None:
    pf = load_prompt(_write(tmp_path, _MINIMAL))
    with pytest.raises(PromptError, match="missing template vars"):
        pf.render()


def test_undeclared_body_var_fails_at_load(tmp_path: Path) -> None:
    # The body uses {{ who }} but never declares it under input:.
    bad = _MINIMAL.replace("input: [who]", "input: []")
    with pytest.raises(PromptError, match="undeclared template vars"):
        load_prompt(_write(tmp_path, bad))


def test_unknown_strength_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="unknown strength"):
        load_prompt(_write(tmp_path, _MINIMAL.replace("strength: low", "strength: turbo")))


def test_missing_required_field_fails(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="missing required field 'version'"):
        load_prompt(_write(tmp_path, _MINIMAL.replace("version: t-v1\n", "")))


def test_missing_frontmatter_fails(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="missing '---' YAML frontmatter"):
        load_prompt(_write(tmp_path, "just a body, no frontmatter"))


def test_trailing_eof_newline_is_not_part_of_the_body(tmp_path: Path) -> None:
    pf = load_prompt(_write(tmp_path, _MINIMAL))
    assert not pf.render(who="w").endswith("\n")  # the conventional EOF newline is hygiene


def test_entity_disambiguate_file_round_trips_to_the_imported_constants() -> None:
    from jbrain.analysis.entities import (
        DISAMBIGUATE_MAX_TOKENS,
        DISAMBIGUATE_SCHEMA,
        DISAMBIGUATE_STRENGTH,
        DISAMBIGUATE_SYSTEM,
    )

    pf = load_prompt(
        Path(__file__).parents[2] / "src/jbrain/analysis/prompts/entity_disambiguate.prompt"
    )
    assert pf.render() == DISAMBIGUATE_SYSTEM
    assert pf.output_schema == DISAMBIGUATE_SCHEMA
    assert pf.config["max_tokens"] == DISAMBIGUATE_MAX_TOKENS
    # The cheap batched resolver runs on the low tier (behaviour-preserving today).
    assert pf.strength == "low" and DISAMBIGUATE_STRENGTH == "low"


def test_vision_files_round_trip_and_run_on_the_vision_tier() -> None:
    from jbrain.ingest.ocr import (
        DESCRIPTION_MAX_TOKENS,
        DESCRIPTION_STRENGTH,
        DESCRIPTION_SYSTEM,
        OCR_MAX_TOKENS,
        OCR_STRENGTH,
        OCR_SYSTEM,
    )

    base = Path(__file__).parents[2] / "src/jbrain/ingest/prompts"
    ocr = load_prompt(base / "vision_ocr.prompt")
    caption = load_prompt(base / "vision_caption.prompt")
    assert ocr.render() == OCR_SYSTEM and ocr.config["max_tokens"] == OCR_MAX_TOKENS
    assert caption.render() == DESCRIPTION_SYSTEM
    assert caption.config["max_tokens"] == DESCRIPTION_MAX_TOKENS
    # Both image tasks declare the vision tier (adapter picks an image model).
    assert ocr.strength == "vision" and OCR_STRENGTH == "vision"
    assert caption.strength == "vision" and DESCRIPTION_STRENGTH == "vision"
