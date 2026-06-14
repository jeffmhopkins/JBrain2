"""The integrate.note prompt, loaded from its co-located `.prompt` file.

Mirrors analysis/prompt.py (note.extract): the prose, tier, token budget, and
version live in `prompts/integrate_note.prompt`; this module is the loader facade
and builds the per-note user text from the note's extraction + graph context.
INTEGRATE_PROMPT_VERSION is stamped on every IntegrationIntent the agent produces.
"""

from pathlib import Path

from jbrain.analysis.extraction import Extraction
from jbrain.llm.promptfile import load_prompt

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "integrate_note.prompt")

INTEGRATE_PROMPT_VERSION: str = _PROMPT.version
INTEGRATE_STRENGTH: str = _PROMPT.strength
INTEGRATE_MAX_TOKENS: int = int(_PROMPT.config["max_tokens"])
# The body declares no template variables — it is the static system prompt.
INTEGRATE_SYSTEM: str = _PROMPT.render()


def build_integrate_prompt(extraction: Extraction, graph_context: str, note_text: str = "") -> str:
    """The per-note user text: the note's raw text (for coreference + surface
    citation), its extraction, and the known graph context — all wrapped as DATA
    (the system prompt's data/instruction boundary). The mention `name` is the
    `mention_ref` the agent's resolutions and facts key on, so the two stay
    aligned end to end."""
    lines = ["Mentions:"]
    for m in extraction.mentions:
        lines.append(f"- {m.name} (kind: {m.kind}; surface: {m.surface_text!r})")
    lines.append("")
    lines.append("Candidate facts:")
    for f in extraction.facts:
        qual = f".{f.qualifier}" if f.qualifier else ""
        obj = f" -> {f.object_entity_ref}" if f.object_entity_ref else ""
        lines.append(
            f"- {f.entity_ref}.{f.predicate}{qual}{obj} [{f.kind}/{f.assertion}]: {f.statement!r}"
        )
    note_block = "\n".join(lines)
    ctx = graph_context.strip() or "(no related entities found in the graph)"
    body = note_text.strip() or "(raw note text unavailable; use the extraction)"
    return (
        "<note_text>\n"
        + body
        + "\n</note_text>\n\n<note_extraction>\n"
        + note_block
        + "\n</note_extraction>\n\n<graph_context>\n"
        + ctx
        + "\n</graph_context>"
    )
