"""The .prompt loader: frontmatter parsing, strict rendering, and fail-fast
validation. The real note_extract.prompt round-trips to the constants the
pipeline imports, and a content/version guard makes prose drift deliberate."""

import hashlib
import json
from pathlib import Path

import pytest

from jbrain.analysis.prompt import EXTRACTION_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT
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


def test_note_extract_file_round_trips_to_the_imported_constants() -> None:
    pf = load_prompt(Path(__file__).parents[2] / "src/jbrain/analysis/prompts/note_extract.prompt")
    assert pf.render(max_facts=pf.config["max_facts"]) == SYSTEM_PROMPT
    assert pf.output_schema == EXTRACTION_SCHEMA
    assert pf.version == PROMPT_VERSION and pf.strength == "high"


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


def test_prompt_content_is_pinned_to_its_version() -> None:
    """A content/version guard: the rendered prompt + schema hash to a pinned
    value. Editing the prompt prose or schema fails this test until you BOTH bump
    `version` in note_extract.prompt AND update the hash here — which keeps
    PROMPT_VERSION (stamped on every fact) honest, so a re-run is a deliberate
    migration, never silent drift."""
    blob = SYSTEM_PROMPT + "\x00" + json.dumps(EXTRACTION_SCHEMA, sort_keys=True)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    assert (PROMPT_VERSION, digest) == (
        "note-extract-v8",
        "ef5125102f4aec5e8cd8a46417920c64354de2eb318ed262ca79ebf70cbf4c9f",
    )
