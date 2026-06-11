// Pipeline lifecycle chip, derived — never stored: ingest_state, the image
// attachments' vision cache (hasExtracts), and the analyzed flag already
// encode where a note sits in the capture → index → OCR → analyze pipeline.
//
// Precedence (first match wins):
//   1. pending/processing      → "indexing…"
//   2. failed                  → "indexing failed"
//   3. analyzed                → no chip (the quiet end-state)
//   4. indexed, image(s) await → "reading image(s)…" (extract job outstanding)
//   5. indexed, not analyzed   → "analyzing…"
// Analysis gates on image extracts, so the sequence is truly one-way — and
// `analyzed` must outrank the awaiting check: the backend's analyze-anyway
// paths (oversized image, OCR retry exhaustion) leave hasExtracts false
// forever, and the chip must not stick on "reading image…" after analysis
// lands. A note-level re-run flips analyzed back to false without
// re-indexing, so the chip resumes at "analyzing…".

export interface LifecycleSource {
  /** null = outbox row or unresolved search preview — never a chip. */
  ingestState: string | null;
  analyzed: boolean;
  attachments: readonly { mediaType: string; hasExtracts: boolean }[];
}

export interface LifecycleChip {
  label: string;
  /** Maps to the existing chip classes: amber chip-pending / rose chip-failed. */
  tone: "pending" | "failed";
}

/** Images whose vision cache is still empty — the extract jobs analysis
 * waits on. Shared by the lifecycle chip, the Analysis tab's gated empty
 * state, and the Attachments manifest summary, so the surfaces agree. */
export function awaitingImageCount(
  attachments: readonly { mediaType: string; hasExtracts: boolean }[],
): number {
  return attachments.filter((a) => a.mediaType.startsWith("image/") && !a.hasExtracts).length;
}

export function lifecycleChip(source: LifecycleSource): LifecycleChip | null {
  if (source.ingestState === "pending" || source.ingestState === "processing") {
    return { label: "indexing…", tone: "pending" };
  }
  if (source.ingestState === "failed") {
    return { label: "indexing failed", tone: "failed" };
  }
  if (source.ingestState !== "indexed") return null;
  if (source.analyzed) return null;
  const awaiting = awaitingImageCount(source.attachments);
  if (awaiting > 0) {
    return { label: awaiting === 1 ? "reading image…" : "reading images…", tone: "pending" };
  }
  return { label: "analyzing…", tone: "pending" };
}
