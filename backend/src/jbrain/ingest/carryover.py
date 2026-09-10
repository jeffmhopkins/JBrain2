"""Which of a note's existing chunks survive a re-ingest, and where the rest point.

`ingest_note` WAS a destroy-and-rebuild: it deleted every chunk of the note and inserted
fresh ones. Five tables hang off `app.chunks.id` and two of them cascade, so that
silently destroyed, on EVERY re-ingest:

* `app.entity_mentions` (ON DELETE CASCADE, `0006:92`) — including `link_method='human'`
  rows, and the id arrays a review reopen replays (`purge.rebuild_spare_mention_ids`).
* `app.wiki_citations` (ON DELETE CASCADE, `0046:159`) — a PUBLISHED revision loses its
  citations, which is COLD_REVIEW_FINDINGS section E item 6. `facts.chunk_id` and
  `temporal_tokens.chunk_id` are ON DELETE SET NULL, so those rows survive but stop
  being citable (`wiki/builder.py:527` INNER JOINs chunks).

That was a latent bug on manual edits. D6 makes it routine: every answered clarification
re-ingests the note, on exactly the notes with the most citations.

A citation cannot be re-anchored AFTER the fact — CASCADE has already deleted the row —
so the repair has to happen while both generations of chunk exist. This module is the
pure half: given the old shapes and the newly built ones, decide

1. **reuse** — a new chunk byte-identical to an old one in every stored column keeps the
   OLD ROW. Nothing referencing it ever notices the re-ingest, its embedding is not
   thrown away, and `resolution_pin` (whose PK contains `chunk_id` and whose
   `occurrence_index` is chunk-relative, so it can be carried no other way) survives.
2. **re-anchor** — an old chunk with no identical twin, but whose span is COVERED by a
   surviving chunk of the same source, hands its references over before it is deleted.
   Covering is the condition that makes the mention spans translatable: they are
   chunk-relative (`analysis.pipeline._locate` returns `text.find(...)`), so the shift
   is exactly `old.char_start - new.char_start` and stays exact.

This is what makes an appended clarification block non-destructive. The append is at the
end of the composed text, so a long note's body chunks are identical (reuse) and a short
note's single paragraph is absorbed into one larger chunk that covers it (re-anchor).
Anything else — the owner rewrote the body, the note moved domain — matches neither rule
and behaves exactly as before: the references go with the chunk. Never worse, and the
common case is now whole.

`resolution_pin` rides on reuse ONLY. Its primary key is
`(note_id, chunk_id, occurrence_index, decision_kind)`, so two old chunks re-anchored
onto one new chunk would collide on it, and `occurrence_index` counts occurrences within
the OLD chunk's text — a number a span shift cannot correct.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ChunkShape:
    """What a chunk IS, for the purpose of deciding it came back unchanged.

    Every stored column of `app.chunks` except six, each excluded on purpose:

    * `id` — the row identity carry-over exists to preserve; comparing it would defeat it.
    * `note_id` — constant across one note's carry-over, so it carries no signal.
    * `seq` — a position in the rebuilt list, so a chunk that merely MOVED is still the
      same chunk and is reused (its row's `seq` is then updated to the new position).
    * `tsv` — generated from `text`, which is compared.
    * `embedding` — derived from `text` under whatever model was current when it was
      written, so it is not evidence about the chunk.
    * `embedding_model` — the stamp on that vector. Excluded for the SAME reason, and it
      is the one exclusion with a consequence: a reused row keeps a vector embedded under
      a model that may no longer be ours, and `embed_note` only fills NULLs, so nothing on
      the ingest path ever revisits it. That is deliberate — throwing a good vector away
      to re-derive it identically would be worse — and the drift it allows is swept by
      `reembed_stale` (`analysis/reembed.py`, the `chunks` target), which is where the
      re-embed-after-a-model-change path now lives for chunks.
    """

    attachment_id: UUID | None
    source_kind: str
    source_anchor: str | None
    granularity: str
    domain_code: str
    char_start: int | None
    char_end: int | None
    text: str

    @property
    def source(self) -> tuple[UUID | None, str, str | None, str, str]:
        """What must match before two chunks are even comparable: the same segment of
        the same source, at the same granularity, in the same domain. Domain is in here
        so a note that MOVED domain re-anchors nothing — `wiki_citations` carries a
        firewall trigger requiring citation.domain = chunk.domain, and its old rows are
        better cascaded away than repointed at a chunk that would violate it."""
        return (
            self.attachment_id,
            self.source_kind,
            self.source_anchor,
            self.granularity,
            self.domain_code,
        )


@dataclass(frozen=True)
class CarryOverPlan:
    # (old index, new index) pairs whose rows are the same chunk: keep the old row and
    # drop the new one.
    reuse: tuple[tuple[int, int], ...]
    # (old index, new index) pairs where the old row is deleted but hands its references
    # to the new one first. Never overlaps `reuse` on the old side.
    reanchor: tuple[tuple[int, int], ...]

    @property
    def reused_old(self) -> frozenset[int]:
        return frozenset(o for o, _ in self.reuse)


def _covers(outer: ChunkShape, inner: ChunkShape) -> bool:
    """`outer` contains `inner`'s span AND the same characters in it.

    The span test alone is not enough, and the gap is the whole point: a REWRITTEN body
    produces a chunk at offset 0 that "covers" the old one and holds nothing of it, so a
    mention moved there would mark the wrong words. Comparing the text makes the shift
    provably exact — the surface the span pointed at is byte-for-byte where it lands.
    """
    if outer.char_start is None or outer.char_end is None:
        return False
    if inner.char_start is None or inner.char_end is None:
        return False
    if outer.char_start > inner.char_start or outer.char_end < inner.char_end:
        return False
    offset = inner.char_start - outer.char_start
    return outer.text[offset : offset + len(inner.text)] == inner.text


def plan_carry_over(old: list[ChunkShape], new: list[ChunkShape]) -> CarryOverPlan:
    """Match the rebuilt chunks against the note's existing ones.

    Identity is matched WITH MULTIPLICITY — one old chunk consumed per new chunk — for
    the reason `_rebuild_mentions` matches spans that way (constraint 7 of the ingest
    plan): a note can legitimately hold two chunks with identical text and offsets (a
    single-paragraph attachment segment repeated, a section twin), and a set-based match
    would fold them together and strand a row.
    """
    pool: dict[ChunkShape, deque[int]] = defaultdict(deque)
    for i, shape in enumerate(old):
        pool[shape].append(i)

    reuse: list[tuple[int, int]] = []
    for j, shape in enumerate(new):
        candidates = pool.get(shape)
        if candidates:
            reuse.append((candidates.popleft(), j))

    matched_old = {o for o, _ in reuse}
    by_source: dict[tuple[UUID | None, str, str | None, str, str], list[int]] = defaultdict(list)
    for j, shape in enumerate(new):
        by_source[shape.source].append(j)

    reanchor: list[tuple[int, int]] = []
    for i, shape in enumerate(old):
        if i in matched_old:
            continue
        covering = [j for j in by_source.get(shape.source, []) if _covers(new[j], shape)]
        if not covering:
            continue
        # The tightest cover is the most faithful home for a span: it is the chunk a
        # fresh extraction would have anchored the same surface to.
        target = min(covering, key=lambda j: (_span(new[j]), j))
        reanchor.append((i, target))

    return CarryOverPlan(reuse=tuple(reuse), reanchor=tuple(reanchor))


def _span(shape: ChunkShape) -> int:
    # Only ever reached for a shape `_covers` already accepted, so both offsets are set.
    return (shape.char_end or 0) - (shape.char_start or 0)
