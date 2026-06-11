// Pipeline lifecycle chip, derived — never stored: ingest_state, the image
// attachments' vision cache (hasExtracts), and the analyzed flag already
// encode where a note sits in the capture → index → OCR → analyze pipeline.
//
// Precedence (first match wins):
//   1. pending/processing      → "indexing…"
//   2. failed                  → "indexing failed"
//   3. indexed, image(s) await → "reading image(s)…" (OCR job outstanding)
//   4. indexed, not analyzed   → "analyzing…"
//   5. analyzed                → no chip (the quiet end-state)
// OCR completion re-ingests then re-analyzes, so after an image lands the
// chip naturally re-walks 1 → 4 — expected, not a bug.

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

export function lifecycleChip(source: LifecycleSource): LifecycleChip | null {
  if (source.ingestState === "pending" || source.ingestState === "processing") {
    return { label: "indexing…", tone: "pending" };
  }
  if (source.ingestState === "failed") {
    return { label: "indexing failed", tone: "failed" };
  }
  if (source.ingestState !== "indexed") return null;
  const awaitingOcr = source.attachments.filter(
    (a) => a.mediaType.startsWith("image/") && !a.hasExtracts,
  ).length;
  if (awaitingOcr > 0) {
    return { label: awaitingOcr === 1 ? "reading image…" : "reading images…", tone: "pending" };
  }
  if (!source.analyzed) return { label: "analyzing…", tone: "pending" };
  return null;
}
