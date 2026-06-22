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
# Facts-per-note budget. The cap scales with note length: a one-line note and a
# long journal entry should not share a 12-fact ceiling — the static cap silently
# truncated the tail of long, fact-dense notes. MAX_FACTS is the absolute hard
# ceiling (server-side abuse/runaway bound, rendered into the system prompt);
# MIN_FACTS the floor that keeps short notes at today's generous budget. The
# per-note value (fact_cap) is communicated in the user prompt AND enforced in
# extraction.parse_extraction — the two MUST agree, so the pipeline computes it
# once and threads it to both.
MAX_FACTS: int = int(_PROMPT.config["max_facts"])
MIN_FACTS: int = int(_PROMPT.config["min_facts"])
# Roughly one durable fact per this many words: a generous CEILING (the model is
# still told "extract less, not more"), not a target. Tuned so a ~90-word note
# lands on the floor and a long entry climbs toward the ceiling.
_WORDS_PER_FACT = 8


def fact_cap(text: str) -> int:
    """The per-note fact budget for `text`, scaled by length and clamped to
    [MIN_FACTS, MAX_FACTS]. A whitespace word count is a deliberately coarse
    proxy — the cap only bounds runaway extraction, it never sets a target."""
    return max(MIN_FACTS, min(MAX_FACTS, len(text.split()) // _WORDS_PER_FACT))


# Character budget per extraction group (~1500 tokens of body). A note whose
# whole content fits stays ONE group — one call, exactly as before; a long note
# (a pasted article, a medical-history dump) fans out so its per-group fact
# budget is never the bottleneck and a single call's output-token ceiling
# (EXTRACT_MAX_TOKENS) is never the limit on how much the note can yield. Well
# under that ceiling so each group's facts JSON always fits its own response.
GROUP_CHAR_BUDGET = 6000


def group_texts(texts: list[str], budget: int = GROUP_CHAR_BUDGET) -> list[list[str]]:
    """Partition prompt-block texts into ordered groups for chunk-level
    map-reduce extraction, each (besides a lone oversize block) under `budget`
    characters. Greedy and order-preserving; never splits a block — paragraph
    chunks are the atomic citation unit. A note that fits in `budget` yields
    exactly one group, so short notes keep the single-call path unchanged."""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        if current and size + len(text) > budget:
            groups.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        groups.append(current)
    return groups or [[]]


EXTRACT_MAX_TOKENS: int = int(_PROMPT.config["max_tokens"])
SYSTEM_PROMPT: str = _PROMPT.render(max_facts=MAX_FACTS)
EXTRACTION_SCHEMA: dict[str, Any] = _PROMPT.output_schema or {}
# Domain-conditioned guidance appended to the per-note prompt by capture domain
# (build_user_prompt). Fixes the entity SHAPE for health/finance notes; per-fact
# domain CLASSIFICATION the model already does well.
DOMAIN_GUIDANCE: dict[str, str] = dict(_PROMPT.domain_guidance)


# An audio transcript whose words' mean confidence sits below this reads as
# "low-confidence" in its marker, so the model discounts facts built on it harder
# than a clean transcription (the analysis half of the per-word data the UI colors).
TRANSCRIPT_LOW_CONFIDENCE = 0.6


def prompt_block(
    text: str, *, source_kind: str, filename: str | None, confidence: float | None = None
) -> str:
    """One chunk as the extraction model sees it.

    OCR, caption, and transcript chunks announce their provenance: the system
    prompt's confidence rule ("lower it for garbled, OCR-derived,
    audio-transcribed, or uncertain content") only fires if the model can TELL the
    text is machine-read — nothing else in the concatenated note content conveys
    it. Facts from these blocks then inherit reduced confidence, which is what
    keeps a misread health number from auto-superseding anything (docs/ANALYSIS.md
    "Guards"). A transcript additionally carries a "low-confidence" qualifier when
    its measured confidence was low, so the model discounts a noisy clip harder."""
    name = filename or "attachment"
    if source_kind == "ocr":
        return f"[ocr from {name}]\n{text}"
    if source_kind == "caption":
        return f"[image caption of {name}]\n{text}"
    if source_kind == "transcript":
        low = confidence is not None and confidence < TRANSCRIPT_LOW_CONFIDENCE
        return f"[{'low-confidence ' if low else ''}transcript from {name}]\n{text}"
    return text


def build_user_prompt(
    texts: list[str], *, anchor: datetime, domain: str, max_facts: int = MAX_FACTS
) -> str:
    """The per-note prompt: capture anchor (with timezone — the resolution
    target for every relative phrase), capture domain, the per-note fact budget,
    and the chunk texts. `max_facts` defaults to the ceiling so callers that
    don't scale it still get a valid budget; the pipeline passes fact_cap(...)."""
    content = "\n\n".join(t for t in texts if t.strip())
    guidance = DOMAIN_GUIDANCE.get(domain)
    block = f"\n{guidance}\n" if guidance else ""
    return (
        f"Capture anchor (note creation time): {anchor.isoformat()}\n"
        f"Note capture domain: {domain}\n"
        f"Fact budget for this note: at most {max_facts} facts "
        f"(a ceiling, not a target — capture every real fact the note states, but do "
        f"not pad).\n{block}\n"
        f"Note content:\n{content}"
    )
