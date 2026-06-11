// The Analysis tab's state matrix and Sources card (settled review —
// variant B): facts + pipeline provenance, the relocated image-extract
// expansion, the gated empty state, and the note-level re-run poller.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AttachmentExtract, NoteAnalysis } from "../api/client";
import type { StreamAttachment } from "../notes/useNotes";
import { AnalysisTab } from "./AnalysisTab";

const PDF: StreamAttachment = {
  id: "a1",
  filename: "lab-orders.pdf",
  mediaType: "application/pdf",
  sizeBytes: 24_120,
  hasExtracts: false,
  hasDescription: false,
};

const IMG_DONE: StreamAttachment = {
  id: "a3",
  filename: "whiteboard.jpg",
  mediaType: "image/jpeg",
  sizeBytes: 300_000,
  hasExtracts: true,
  hasDescription: true,
};

const IMG_OCR_ONLY: StreamAttachment = {
  id: "a4",
  filename: "scan.png",
  mediaType: "image/png",
  sizeBytes: 100_000,
  hasExtracts: true,
  hasDescription: false,
};

const IMG_PENDING: StreamAttachment = {
  id: "a2",
  filename: "receipt.png",
  mediaType: "image/png",
  sizeBytes: 512_000,
  hasExtracts: false,
  hasDescription: false,
};

const ANALYZED: NoteAnalysis = {
  note_id: "n1",
  title: "Whiteboard decisions",
  tags: ["planning"],
  analyzed_at: "2026-06-11T08:02:00Z",
  extractor: "xai:grok-4.3",
  facts: [
    {
      id: "f1",
      entity_id: "ent-me",
      entity_name: "Me",
      predicate: "owns",
      qualifier: null,
      kind: "state",
      statement: "Jeff owns the brain database system.",
      value_json: "brain database system",
      assertion: "asserted",
      status: "active",
      pinned: false,
      confidence: 0.96,
      valid_from: null,
      valid_to: null,
      reported_at: "2026-06-11T08:02:00Z",
      temporal_precision: "day",
      source_snippet: null,
    },
  ],
  entities: [{ id: "ent-me", kind: "Person", name: "Me", status: "active" }],
  temporal_tokens: [],
};

const FRESH: NoteAnalysis = {
  ...ANALYZED,
  title: "Whiteboard decisions — re-read",
  analyzed_at: "2026-06-11T09:30:00Z",
};

const NOT_ANALYZED: NoteAnalysis = {
  ...ANALYZED,
  title: null,
  tags: [],
  analyzed_at: null,
  extractor: null,
  facts: [],
  entities: [],
};

function extract(kind: "ocr" | "caption", text: string): AttachmentExtract {
  return {
    kind,
    text,
    tool: "xai:grok-4.3",
    confidence: kind === "ocr" ? 0.7 : 0.6,
    created_at: "2026-06-11T09:00:00.000Z",
  };
}

const OCR_8_LINES = [
  "Q3 PLANNING",
  "- ship phase 4 conversations",
  "- [illegible] retrieval evals",
  "- ocr -> facts pipeline",
  "owner: jeff",
  "demo [illegible] 6/19",
  "follow up with sam",
  "budget: tbd",
].join("\n");

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface StubOpts {
  analysis: NoteAnalysis;
  /** Served once `landed` flips (an analyze POST, or land() in tests). */
  fresh?: NoteAnalysis;
  mode?: "full" | "ocr";
  extracts?: Record<string, AttachmentExtract[]>;
  freshExtracts?: Record<string, AttachmentExtract[]>;
  /** Status for POST /notes/n1/analyze (default 202; 409 reads the same). */
  noteAnalyzeStatus?: number;
  analysisError?: boolean;
}

/** Route table mirroring everything the tab touches; `landed` simulates the
 * worker finishing — later analysis/extract reads serve the fresh values. */
function stubApi(opts: StubOpts) {
  let landed = false;
  const analyzedAttachments: string[] = [];
  const noteAnalyzePosts: number[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url === "/api/notes/n1/analysis" && method === "GET") {
      if (opts.analysisError) return jsonResponse({ detail: "boom" }, 500);
      return jsonResponse(landed && opts.fresh ? opts.fresh : opts.analysis);
    }
    if (url === "/api/settings" && method === "GET") {
      return jsonResponse({ image_analysis_mode: opts.mode ?? "full" });
    }
    const extractsMatch = url.match(/^\/api\/attachments\/([^/]+)\/extracts$/);
    if (extractsMatch && method === "GET") {
      const table = landed && opts.freshExtracts ? opts.freshExtracts : opts.extracts;
      return jsonResponse({ extracts: table?.[extractsMatch[1] ?? ""] ?? [] });
    }
    const analyzeMatch = url.match(/^\/api\/attachments\/([^/]+)\/analyze$/);
    if (analyzeMatch && method === "POST") {
      analyzedAttachments.push(analyzeMatch[1] ?? "");
      landed = true;
      return jsonResponse({ job_id: "job-1" }, 202);
    }
    if (url === "/api/notes/n1/analyze" && method === "POST") {
      const status = opts.noteAnalyzeStatus ?? 202;
      noteAnalyzePosts.push(status);
      landed = true; // even a 409 means some run is in flight and will land
      return status === 202
        ? jsonResponse({ job_id: "job-2" }, 202)
        : jsonResponse({ detail: "analysis already queued or running" }, status);
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    analyzedAttachments,
    noteAnalyzePosts,
    land: () => {
      landed = true;
    },
  };
}

function renderTab(over: Partial<Parameters<typeof AnalysisTab>[0]> = {}) {
  const onOpenEntity = vi.fn();
  render(
    <AnalysisTab
      noteId="n1"
      attachments={[PDF, IMG_DONE]}
      ingestState="indexed"
      bodyChars={161}
      onOpenEntity={onOpenEntity}
      {...over}
    />,
  );
  return { onOpenEntity };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("AnalysisTab states", () => {
  it("renders facts plus the Sources card: note-text row, ✓ stages, provenance footer", async () => {
    stubApi({
      analysis: ANALYZED,
      extracts: { a3: [extract("ocr", "jbrain v2 arch"), extract("caption", "a whiteboard.")] },
    });
    renderTab();

    expect(await screen.findByText("Whiteboard decisions")).toBeInTheDocument();
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("note text")).toBeInTheDocument();
    expect(screen.getByText("161 chars · the note body itself")).toBeInTheDocument();

    // Only images get pipeline rows — the pdf is Attachments-tab business.
    expect(screen.getByText("whiteboard.jpg")).toBeInTheDocument();
    expect(screen.queryByText("lab-orders.pdf")).not.toBeInTheDocument();

    // Both stages settle to ✓ once the eager extract fetch resolves.
    await waitFor(() => expect(document.querySelectorAll(".stage-done")).toHaveLength(2));

    // The provenance footer owns the re-run action, enabled when analyzed.
    expect(screen.getByText(/analyzed .*xai:grok-4\.3/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "re-run analysis" })).toBeEnabled();
  });

  it("collapses to the note-text row + footer when the note has no images", async () => {
    stubApi({ analysis: ANALYZED });
    renderTab({ attachments: [PDF] });

    expect(await screen.findByText("Whiteboard decisions")).toBeInTheDocument();
    expect(screen.getByText("note text")).toBeInTheDocument();
    expect(screen.getByText(/analyzed .*xai:grok-4\.3/)).toBeInTheDocument();
    // No image rows: nothing expandable in the card.
    expect(document.querySelector(".analysis-sources-row[role='button']")).toBeNull();
    expect(document.querySelector(".analysis-sources-caret")).toBeNull();
  });

  it("plain not-analyzed (no images outstanding) keeps the quiet line, no card", async () => {
    stubApi({ analysis: NOT_ANALYZED });
    renderTab({ attachments: [PDF] });
    expect(
      await screen.findByText("analysis runs after indexing — nothing here yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText("note text")).not.toBeInTheDocument();
  });

  it("a load failure shows the quiet error line", async () => {
    stubApi({ analysis: ANALYZED, analysisError: true });
    renderTab({ attachments: [] });
    expect(
      await screen.findByText("couldn't load analysis — reopen to retry."),
    ).toBeInTheDocument();
  });

  it("an unsynced note (null id) shows the quiet line without fetching", () => {
    const { fetchMock } = stubApi({ analysis: ANALYZED });
    renderTab({ noteId: null, attachments: [] });
    expect(
      screen.getByText("analysis runs after indexing — nothing here yet."),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("gated: waiting line + mid-flight sources + disabled re-run, until the poller lands", async () => {
    const stub = stubApi({
      analysis: NOT_ANALYZED,
      fresh: FRESH,
      extracts: {},
      freshExtracts: { a2: [extract("ocr", "milk, eggs"), extract("caption", "a receipt.")] },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTab({ attachments: [IMG_PENDING] });

    expect(
      await screen.findByText(
        "waiting on image analysis — facts extract once every source below is in.",
      ),
    ).toBeInTheDocument();
    // The facts area is absent; the card runs mid-flight with the gate named.
    expect(screen.queryByText("Whiteboard decisions")).not.toBeInTheDocument();
    expect(
      screen.getByText("analysis waits here — runs automatically when every source is in."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "re-run analysis" })).toBeDisabled();
    await waitFor(() =>
      expect(document.querySelectorAll(".stage-running").length).toBeGreaterThan(0),
    );

    // The backend finishes the gate + analysis; the next poll tick swaps in
    // the fresh result without reopening the note.
    stub.land();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(screen.getByText("Whiteboard decisions — re-read")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "re-run analysis" })).toBeEnabled();
    await waitFor(() => expect(document.querySelectorAll(".stage-done")).toHaveLength(2));
  });
});

describe("Sources image expansion (relocated from Attachments)", () => {
  it("unfolds in place: clamped verbatim OCR, illegible muted, show-all, description provenance", async () => {
    stubApi({
      analysis: ANALYZED,
      extracts: {
        a3: [
          extract("ocr", OCR_8_LINES),
          extract("caption", "a whiteboard of q3 planning bullets, partly smudged."),
        ],
      },
    });
    renderTab();
    await screen.findByText("whiteboard.jpg");
    await waitFor(() => expect(document.querySelectorAll(".stage-done")).toHaveLength(2));

    const row = screen.getByText("whiteboard.jpg").closest(".analysis-sources-row");
    expect(row).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(screen.getByText("whiteboard.jpg"));
    expect(row).toHaveAttribute("aria-expanded", "true");

    // Verbatim OCR, clamped; the honesty marker renders muted-italic.
    expect(await screen.findByText(/Q3 PLANNING/)).toBeInTheDocument();
    const pre = document.querySelector(".x-text");
    expect(pre).not.toHaveClass("all");
    expect(document.querySelectorAll(".x-illegible")).toHaveLength(2);

    // 8 lines exceed the ~6-line clamp: show-all grows in place.
    fireEvent.click(screen.getByRole("button", { name: "show all 8 lines" }));
    expect(document.querySelector(".x-text")).toHaveClass("all");
    fireEvent.click(screen.getByRole("button", { name: "show less" }));
    expect(document.querySelector(".x-text")).not.toHaveClass("all");

    // The description beneath, with its mined-for-facts provenance line.
    expect(
      screen.getByText("a whiteboard of q3 planning bullets, partly smudged."),
    ).toBeInTheDocument();
    expect(screen.getByText("ocr · xai:grok-4.3 · 70%")).toBeInTheDocument();
    expect(
      screen.getByText("caption · xai:grok-4.3 · 60% · mined for facts in analysis"),
    ).toBeInTheDocument();

    // Tapping the row again folds it back.
    fireEvent.click(screen.getByText("whiteboard.jpg"));
    expect(screen.queryByText(/Q3 PLANNING/)).not.toBeInTheDocument();
  });

  it("ocr-only mode: the stage line reads skipped and the expansion says why", async () => {
    stubApi({
      analysis: ANALYZED,
      mode: "ocr",
      extracts: { a4: [extract("ocr", "RIDGELINE SCAN\nTOTAL 4,200")] },
    });
    renderTab({ attachments: [IMG_OCR_ONLY] });
    await screen.findByText("scan.png");
    await waitFor(() => expect(screen.getByText("skipped")).toBeInTheDocument());

    fireEvent.click(screen.getByText("scan.png"));
    expect(await screen.findByText(/RIDGELINE SCAN/)).toBeInTheDocument();
    // Two lines fit the clamp — nothing to grow.
    expect(screen.queryByRole("button", { name: /show all/ })).not.toBeInTheDocument();
    expect(
      screen.getByText("no description — image analysis is set to ocr only."),
    ).toBeInTheDocument();
  });

  it("⋯ → re-run image analysis POSTs, reads calm in flight, and polls the result in", async () => {
    const stub = stubApi({
      analysis: ANALYZED,
      fresh: FRESH,
      extracts: { a4: [extract("ocr", "RIDGELINE SCAN\nTOTAL 4,200")] },
      freshExtracts: {
        a4: [
          extract("ocr", "RIDGELINE SCAN\nTOTAL 4,200"),
          extract("caption", "a scanned contractor quote."),
        ],
      },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTab({ attachments: [IMG_OCR_ONLY] });
    await screen.findByText("scan.png");

    fireEvent.click(screen.getByRole("button", { name: "Actions for scan.png" }));
    fireEvent.click(screen.getByRole("button", { name: "re-run image analysis" }));
    await waitFor(() => expect(stub.analyzedAttachments).toEqual(["a4"]));
    // Picking the action closes the sheet.
    expect(screen.queryByRole("button", { name: "re-run image analysis" })).not.toBeInTheDocument();

    // In flight: the expansion shows the calm line, the stage spins.
    fireEvent.click(screen.getByText("scan.png"));
    expect(await screen.findByText("analyzing image…")).toBeInTheDocument();
    expect(document.querySelectorAll(".stage-running").length).toBeGreaterThan(0);

    // The next poll tick sees the bumped analyzed_at and the result fills in
    // without reopening the note.
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(await screen.findByText("a scanned contractor quote.")).toBeInTheDocument();
    expect(screen.queryByText("analyzing image…")).not.toBeInTheDocument();
    expect(screen.getByText("Whiteboard decisions — re-read")).toBeInTheDocument();
  });
});

describe("note-level re-run", () => {
  it("POSTs analyze, disables into re-running…, then swaps the fresh analysis in", async () => {
    const stub = stubApi({ analysis: ANALYZED, fresh: FRESH });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTab({ attachments: [] });
    await screen.findByText("Whiteboard decisions");

    fireEvent.click(screen.getByRole("button", { name: "re-run analysis" }));
    await waitFor(() => expect(stub.noteAnalyzePosts).toEqual([202]));
    expect(screen.getByRole("button", { name: /re-running…/ })).toBeDisabled();

    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(screen.getByText("Whiteboard decisions — re-read")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "re-run analysis" })).toBeEnabled();
  });

  it("a 409 (run already in flight) reads the same and still picks up the result", async () => {
    const stub = stubApi({ analysis: ANALYZED, fresh: FRESH, noteAnalyzeStatus: 409 });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTab({ attachments: [] });
    await screen.findByText("Whiteboard decisions");

    fireEvent.click(screen.getByRole("button", { name: "re-run analysis" }));
    await waitFor(() => expect(stub.noteAnalyzePosts).toEqual([409]));
    expect(screen.getByRole("button", { name: /re-running…/ })).toBeDisabled();

    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(screen.getByText("Whiteboard decisions — re-read")).toBeInTheDocument();
  });
});
