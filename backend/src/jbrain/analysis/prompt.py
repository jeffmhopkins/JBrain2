"""What outlived `note.extract`: the version stamped on every fact, and the per-note
fact budget the parse enforces.

R4 deleted the note.extract prompt, its schema and the chain that called it. Two things
it carried are NOT the prompt's and did not die with it:

**PROMPT_VERSION** is stamped on `facts.prompt_version` and `note_analysis.prompt_version`
— what makes a corpus re-run a planned, budgeted migration instead of silent drift. The
column stays; its SOURCE moves to the producer that writes the graph now, the
note-conversation persona, read the same way from its own prompt file's frontmatter. Bump
it there (`agent/prompts/note_ingest.prompt`) whenever that persona's contract changes.

**The fact budget** bounds a runaway reading. It was the extraction prompt's `config`
block; with the prompt gone the numbers are constants here, and they are still exactly
what `extraction.parse_extraction` and `analysis.intent_parse`-shaped bounds enforce. It
is a CEILING, never a target.
"""

from pathlib import Path

from jbrain.llm.promptfile import load_prompt

# The note-conversation persona: the producer whose reading becomes the graph. Loaded by
# path rather than through `agent.agents` on purpose — this module is imported from the
# analysis write path, and the agent registry pulls in the whole tool surface.
_NOTE_INGEST = load_prompt(Path(__file__).parents[1] / "agent" / "prompts" / "note_ingest.prompt")

PROMPT_VERSION: str = _NOTE_INGEST.version

# Absolute hard ceiling on facts parsed out of one note (server-side abuse/runaway
# bound). MIN_FACTS floors the CAP, not the output — it keeps a dense short note (a
# 40-word family roster) from clipping its tier-1 kinship edges.
MAX_FACTS: int = 40
MIN_FACTS: int = 6
# Roughly one durable fact per this many words: a generous CEILING, not a target. A
# ~40-word note lands on the floor; a long entry climbs toward the ceiling.
_WORDS_PER_FACT = 8


def fact_cap(text: str) -> int:
    """The per-note fact budget for `text`, scaled by length and clamped to
    [MIN_FACTS, MAX_FACTS]. A whitespace word count is a deliberately coarse
    proxy — the cap only bounds runaway extraction, it never sets a target."""
    return max(MIN_FACTS, min(MAX_FACTS, len(text.split()) // _WORDS_PER_FACT))
