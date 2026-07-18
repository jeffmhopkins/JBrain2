"""Timeline windower — turn a VideoAnalysis's structured frames + utterances into
time-coherent, clean-prose passages for the external-source corpus.

`chunker.chunk_text` cannot be reused here: it works on char offsets with no notion of
time, and the rendered `[mm:ss]` timeline (`ingest.video.build_timeline`) has no blank
lines — so it would collapse into one giant paragraph hard-cut at arbitrary sentence
boundaries — while its `[mm:ss]`/`(frame)`/`(said)` markers would pollute both the FTS
`tsv` and the embedding vector. This windower groups the STRUCTURED entries instead, so
each passage carries the real millisecond offset of its first entry (for the deep-link)
and marker-free prose (for clean indexing). Pure — no DB, no LLM — so it unit-tests directly.
"""

from dataclasses import dataclass
from typing import Any

from jbrain.ingest.video import group_utterances

# A passage caps near this many characters (the chunker's paragraph ceiling), and a gap
# larger than this many ms starts a new passage so one window never straddles a long jump
# in the video (a stretch the sampler/transcript skipped over).
TARGET_CHARS = 1200
GAP_MS = 30_000


@dataclass(frozen=True)
class TimelineWindow:
    """One searchable passage: its ordinal, the real ms offset of its first entry, and
    clean marker-free prose."""

    seq: int
    t_ms: int
    text: str


def window_timeline(
    analysis: dict[str, Any], *, target_chars: int = TARGET_CHARS, gap_ms: int = GAP_MS
) -> list[TimelineWindow]:
    """Group the analysis's frame captions and grouped utterances into time-coherent
    passages. Entries are merged and time-ordered (a frame before speech at the same
    instant — what's on screen frames the line that's said), then split when adding the
    next entry would exceed `target_chars` or a gap larger than `gap_ms` opens."""
    frames = analysis.get("frames") or []
    transcript = analysis.get("transcript")
    words = list(transcript.get("words") or []) if isinstance(transcript, dict) else []

    entries: list[tuple[int, int, str]] = []
    for f in frames:
        caption = str(f.get("caption", "")).strip()
        if caption:
            entries.append((int(f["t_ms"]), 0, caption))
    for u in group_utterances(words):
        spoken = str(u.get("text", "")).strip()
        if spoken:
            entries.append((int(u["t_ms"]), 1, spoken))
    entries.sort(key=lambda e: (e[0], e[1]))

    windows: list[TimelineWindow] = []
    buf: list[str] = []
    start_ms = 0
    prev_ms: int | None = None

    def flush() -> None:
        nonlocal buf
        if buf:
            windows.append(TimelineWindow(seq=len(windows), t_ms=start_ms, text=" ".join(buf)))
            buf = []

    for t_ms, _, text in entries:
        would_exceed = buf and sum(len(x) + 1 for x in buf) + len(text) > target_chars
        big_gap = buf and prev_ms is not None and t_ms - prev_ms > gap_ms
        if would_exceed or big_gap:
            flush()
        if not buf:
            start_ms = t_ms
        buf.append(text)
        prev_ms = t_ms
    flush()
    return windows
