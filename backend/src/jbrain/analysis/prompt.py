"""The note.extract prompt, loaded from its co-located `.prompt` file.

The prose, output JSON schema, token budget, capability tier, and version now
live in `prompts/note_extract.prompt` (YAML frontmatter + body) — easier to view
and edit than an in-code constant, and one artifact carries all of it. This
module is the loader facade: it renders the file at import and re-exposes the
same names the pipeline, evals, harness, and tests already import, so the
externalization is invisible to callers.

PROMPT_VERSION is stamped on every fact and on note_analysis: it is what makes a
corpus re-run a planned, budgeted migration instead of silent drift. It lives in
the file's frontmatter now; bump it there whenever the prompt or schema changes
meaningfully (a CI guard fails if the prose changes without a bump).
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from jbrain.llm.promptfile import load_prompt

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "note_extract.prompt")

PROMPT_VERSION: str = _PROMPT.version
# Capability tier the router resolves to a concrete model (jbrain.llm.router).
NOTE_EXTRACT_STRENGTH: str = _PROMPT.strength
# Facts-per-note cap: taught in the prompt body (rendered below) and enforced
# server-side in extraction.parse_extraction.
MAX_FACTS: int = int(_PROMPT.config["max_facts"])
EXTRACT_MAX_TOKENS: int = int(_PROMPT.config["max_tokens"])
SYSTEM_PROMPT: str = _PROMPT.render(max_facts=MAX_FACTS)
EXTRACTION_SCHEMA: dict[str, Any] = _PROMPT.output_schema or {}
# Domain-conditioned guidance appended to the per-note prompt by capture domain
# (build_user_prompt). Fixes the entity SHAPE for health/finance notes; per-fact
# domain CLASSIFICATION the model already does well.
DOMAIN_GUIDANCE: dict[str, str] = dict(_PROMPT.domain_guidance)


def prompt_block(text: str, *, source_kind: str, filename: str | None) -> str:
    """One chunk as the extraction model sees it.

    OCR and caption chunks announce their provenance: the system prompt's
    confidence rule ("lower it for garbled, OCR-derived, or inferred
    content") only fires if the model can TELL the text is machine-read —
    nothing else in the concatenated note content conveys it. Facts from
    these blocks then inherit reduced confidence, which is what keeps a
    misread health number from auto-superseding anything (docs/ANALYSIS.md
    "Guards")."""
    name = filename or "attachment"
    if source_kind == "ocr":
        return f"[ocr from {name}]\n{text}"
    if source_kind == "caption":
        return f"[image caption of {name}]\n{text}"
    return text


def build_user_prompt(texts: list[str], *, anchor: datetime, domain: str) -> str:
    """The per-note prompt: capture anchor (with timezone — the resolution
    target for every relative phrase), capture domain, and the chunk texts."""
    content = "\n\n".join(t for t in texts if t.strip())
    guidance = DOMAIN_GUIDANCE.get(domain)
    block = f"\n{guidance}\n" if guidance else ""
    return (
        f"Capture anchor (note creation time): {anchor.isoformat()}\n"
        f"Note capture domain: {domain}\n{block}\n"
        f"Note content:\n{content}"
    )
