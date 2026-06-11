import { describe, expect, it } from "vitest";
import { type LifecycleSource, awaitingImageCount, lifecycleChip } from "./lifecycle";

function image(hasExtracts: boolean) {
  return { mediaType: "image/jpeg", hasExtracts };
}

function pdf() {
  return { mediaType: "application/pdf", hasExtracts: true };
}

function source(overrides: Partial<LifecycleSource> = {}): LifecycleSource {
  return { ingestState: "indexed", analyzed: false, attachments: [], ...overrides };
}

describe("lifecycleChip", () => {
  it("pending and processing both read as indexing", () => {
    for (const ingestState of ["pending", "processing"]) {
      expect(lifecycleChip(source({ ingestState }))).toEqual({
        label: "indexing…",
        tone: "pending",
      });
    }
  });

  it("failed ingestion is the rose chip", () => {
    expect(lifecycleChip(source({ ingestState: "failed" }))).toEqual({
      label: "indexing failed",
      tone: "failed",
    });
  });

  it("indexing outranks the OCR wait — chips never run backwards", () => {
    expect(lifecycleChip(source({ ingestState: "pending", attachments: [image(false)] }))).toEqual({
      label: "indexing…",
      tone: "pending",
    });
  });

  it("an indexed note with an un-OCR'd image is reading it", () => {
    expect(lifecycleChip(source({ attachments: [pdf(), image(false)] }))).toEqual({
      label: "reading image…",
      tone: "pending",
    });
  });

  it("two or more awaiting images pluralize the label", () => {
    expect(lifecycleChip(source({ attachments: [image(false), image(false)] }))).toEqual({
      label: "reading images…",
      tone: "pending",
    });
  });

  it("analyzed outranks the awaiting check — analyze-anyway paths leave hasExtracts false forever", () => {
    expect(lifecycleChip(source({ analyzed: true, attachments: [image(false)] }))).toBeNull();
  });

  it("a note-level re-run (analyzed back to false, extracts cached) resumes at analyzing…", () => {
    expect(lifecycleChip(source({ analyzed: false, attachments: [image(true)] }))).toEqual({
      label: "analyzing…",
      tone: "pending",
    });
  });

  it("indexed with no attachments skips straight to analyzing", () => {
    expect(lifecycleChip(source())).toEqual({ label: "analyzing…", tone: "pending" });
  });

  it("cached images don't hold the note in the OCR state", () => {
    expect(lifecycleChip(source({ attachments: [image(true), pdf()] }))).toEqual({
      label: "analyzing…",
      tone: "pending",
    });
  });

  it("analyzed is the quiet end-state — no chip", () => {
    expect(lifecycleChip(source({ analyzed: true }))).toBeNull();
    expect(lifecycleChip(source({ analyzed: true, attachments: [image(true)] }))).toBeNull();
  });

  it("a null ingest state (outbox row, search preview) never chips", () => {
    expect(lifecycleChip(source({ ingestState: null }))).toBeNull();
    expect(lifecycleChip(source({ ingestState: null, attachments: [image(false)] }))).toBeNull();
  });
});

describe("awaitingImageCount", () => {
  it("counts only images with an empty vision cache", () => {
    expect(awaitingImageCount([])).toBe(0);
    expect(awaitingImageCount([pdf(), image(true)])).toBe(0);
    expect(awaitingImageCount([pdf(), image(false), image(false), image(true)])).toBe(2);
  });
});
