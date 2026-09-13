"""What outlived `note.extract`: the version stamped on every fact, the per-note fact
budget the parse enforces, and the marker that says a block of a note was read by a
machine.

R4 deleted the note.extract prompt, its schema and the chain that called it. Three things
it carried are NOT the prompt's and did not die with it:

**PROMPT_VERSION** is stamped on `facts.prompt_version` and `note_analysis.prompt_version`
— what makes a corpus re-run a planned, budgeted migration instead of silent drift. The
column stays; its SOURCE moves to the producer that writes the graph now, the
note-conversation persona, read the same way from its own prompt file's frontmatter. Bump
it there (`agent/prompts/note_ingest.prompt`) whenever that persona's contract changes.

**The fact budget** bounds a runaway reading. It was the extraction prompt's `config`
block; with the prompt gone the numbers are constants here, and they are still what
`extraction.parse_extraction` enforces. It is a CEILING, never a target.

**`prompt_block`** marks an attachment's OCR / caption / transcript text where it is
concatenated into the note a reader is given. The deleted chain built the note out of
paragraph CHUNKS and marked each one; the note conversation composes the same text in
`converse.NoteConverseRunner._note_text`, which is what keeps a photographed receipt
saying something to the graph.
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


# An audio transcript whose words' mean confidence sits below this reads as
# "low-confidence" in its marker, so the reader discounts facts built on it harder
# than a clean transcription (the analysis half of the per-word data the UI colors).
TRANSCRIPT_LOW_CONFIDENCE = 0.6


def prompt_block(
    text: str, *, source_kind: str, filename: str | None, confidence: float | None = None
) -> str:
    """One machine-read attachment block as the reader sees it.

    OCR, caption, transcript and video-analysis text announce their provenance: the
    persona's confidence rule ("lower it for garbled, OCR-derived, audio-transcribed, or
    uncertain content") only fires if the model can TELL the text is machine-read —
    nothing else in the concatenated note content conveys it. Facts from these blocks
    then inherit reduced confidence, which is what keeps a misread health number from
    auto-superseding anything (docs/reference/ANALYSIS.md "Guards"). A transcript
    additionally carries a "low-confidence" qualifier when its measured confidence was
    low, so the reader discounts a noisy clip harder."""
    name = filename or "attachment"
    if source_kind == "ocr":
        return f"[ocr from {name}]\n{text}"
    if source_kind == "caption":
        return f"[image caption of {name}]\n{text}"
    if source_kind == "transcript":
        low = confidence is not None and confidence < TRANSCRIPT_LOW_CONFIDENCE
        return f"[{'low-confidence ' if low else ''}transcript from {name}]\n{text}"
    if source_kind == "video_analysis":
        # A machine-watched summary, not the author's words — mark it so facts mined
        # from it inherit the same reduced confidence as OCR/transcript (Guards).
        return f"[video analysis of {name}]\n{text}"
    return text
