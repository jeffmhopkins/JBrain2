import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NoteAnalysis, SearchResult } from "../api/client";
import type { StreamItem } from "../notes/useNotes";
import { NoteScreen, noteViewFromItem, noteViewFromSearch } from "./NoteScreen";

const ITEM: StreamItem = {
  key: "k1",
  id: "n1",
  domain: "health",
  destination: "Labs",
  body: "first paragraph\n\nsecond paragraph",
  createdAt: new Date(2026, 5, 9, 10, 5),
  ingestState: "pending",
  attachments: [
    {
      id: "a1",
      filename: "lab-orders.pdf",
      mediaType: "application/pdf",
      sizeBytes: 24_120,
      hasExtracts: false,
    },
  ],
  pending: false,
};

// Indexed variant with a PDF (searchable), an image still waiting on the
// async OCR job, and an image whose vision cache is already filled.
const INDEXED: StreamItem = {
  ...ITEM,
  ingestState: "indexed",
  attachments: [
    ...ITEM.attachments,
    {
      id: "a2",
      filename: "receipt.png",
      mediaType: "image/png",
      sizeBytes: 512_000,
      hasExtracts: false,
    },
    {
      id: "a3",
      filename: "whiteboard.jpg",
      mediaType: "image/jpeg",
      sizeBytes: 300_000,
      hasExtracts: true,
    },
  ],
};

function setup(
  source = noteViewFromItem(ITEM),
  resolve: (id: string) => Promise<StreamItem | null> = vi.fn(async () => null),
) {
  const handlers = {
    onClose: vi.fn(),
    onEdit: vi.fn(),
    onMove: vi.fn(),
    onDelete: vi.fn(),
    onAddAttachment: vi.fn(async (_noteId: string, file: File) => ({
      id: "a-new",
      filename: file.name,
      mediaType: file.type || "application/octet-stream",
      sizeBytes: file.size,
      hasExtracts: false,
    })),
    onRemoveAttachment: vi.fn(async () => {}),
    onOpenEntity: vi.fn(),
  };
  render(<NoteScreen source={source} resolve={resolve} syncStatus="synced" {...handlers} />);
  return handlers;
}

const ANALYSIS: NoteAnalysis = {
  note_id: "n1",
  title: "Dr. Patel visit — BP 128/82",
  tags: ["blood-pressure", "dr-patel"],
  analyzed_at: "2026-06-10T09:43:00Z",
  extractor: "xai:grok-4.3 · note.extract v2",
  facts: [
    {
      id: "f1",
      entity_id: "ent-me",
      entity_name: "Me",
      predicate: "blood_pressure",
      qualifier: null,
      kind: "measurement",
      statement: "Blood pressure measured 128/82 mmHg.",
      value_json: { systolic: 128, diastolic: 82, unit: "mmHg" },
      assertion: "asserted",
      status: "active",
      pinned: false,
      confidence: 0.97,
      valid_from: "2026-06-10T09:40:00Z",
      valid_to: null,
      reported_at: "2026-06-10T09:40:00Z",
      temporal_precision: "instant",
      source_snippet: "Saw Dr. Patel — <mark>BP 128/82</mark> this morning",
    },
    {
      id: "f2",
      entity_id: "ent-sarah",
      entity_name: "Sarah",
      predicate: "address",
      qualifier: "home",
      kind: "state",
      statement: "Sarah's home address is in Denver, CO.",
      value_json: "Denver, CO",
      assertion: "asserted",
      status: "pending_review",
      pinned: false,
      confidence: 0.88,
      valid_from: "2026-06-01T00:00:00Z",
      valid_to: null,
      reported_at: "2026-06-10T09:40:00Z",
      temporal_precision: "month",
      source_snippet: "she's mostly <mark>moved into the new Denver place</mark> now",
    },
    {
      id: "f3",
      entity_id: "ent-me",
      entity_name: "Me",
      predicate: "physician",
      qualifier: null,
      kind: "relationship",
      statement: "Dr. Patel is Jeff's physician.",
      value_json: "Dr. Patel",
      assertion: "asserted",
      status: "active",
      pinned: true,
      confidence: 0.99,
      valid_from: null,
      valid_to: null,
      reported_at: "2026-06-10T09:40:00Z",
      temporal_precision: "day",
      source_snippet: null,
    },
    {
      id: "f4",
      entity_id: "ent-me",
      entity_name: "Me",
      predicate: "height",
      qualifier: null,
      kind: "attribute",
      statement: "Jeff is 6'4\" tall.",
      value_json: null,
      assertion: "asserted",
      status: "retracted",
      pinned: false,
      confidence: 0.85,
      valid_from: null,
      valid_to: null,
      reported_at: "2026-06-10T09:40:00Z",
      temporal_precision: "unknown",
      source_snippet: null,
    },
  ],
  entities: [
    { id: "ent-me", kind: "Person", name: "Me", status: "active" },
    { id: "ent-sarah", kind: "Person", name: "Sarah", status: "provisional" },
  ],
  temporal_tokens: [
    {
      id: "tok-1",
      surface_phrase: "in three months (September)",
      kind: "point",
      resolved_start: "2026-09-01T00:00:00Z",
      resolved_end: null,
      temporal_precision: "month",
    },
  ],
};

function stubAnalysisFetch(analysis: NoteAnalysis) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    if (String(input) === "/api/notes/n1/analysis") {
      return new Response(JSON.stringify(analysis), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`Unexpected fetch: ${String(input)}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NoteScreen", () => {
  it("shows domain + date + ingest chip in the header — no title pre-P3", () => {
    setup();
    expect(screen.getByText(/Medical/)).toBeInTheDocument();
    expect(screen.getByText("indexing…")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  it("renders the body as paragraphs; attachments live in their own tab", () => {
    setup();
    expect(screen.getByText("first paragraph")).toBeInTheDocument();
    expect(screen.getByText("second paragraph")).toBeInTheDocument();
    expect(screen.queryByText("lab-orders.pdf")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Attachments/ })).toHaveTextContent("1");
  });

  it("Attachments tab: summary + manifest rows with per-file status chips", () => {
    setup(noteViewFromItem(INDEXED));
    fireEvent.click(screen.getByRole("tab", { name: /Attachments/ }));

    // OCR'd images count as searchable; only the pending one awaits OCR.
    expect(
      screen.getByText("3 files · 817 KB · 2 searchable · 1 awaiting ocr"),
    ).toBeInTheDocument();
    expect(screen.getByText("lab-orders.pdf")).toBeInTheDocument();
    expect(screen.getByText("24 KB · application/pdf")).toBeInTheDocument();
    expect(screen.getByText("text extracted")).toBeInTheDocument();
    // Image pending the async OCR job vs image with a filled vision cache.
    expect(screen.getByText("ocr queued…")).toBeInTheDocument();
    expect(screen.getByText("ocr queued…")).toHaveClass("att-chip-warn");
    expect(screen.getByText("text extracted (ocr)")).toBeInTheDocument();
    expect(screen.getByText("text extracted (ocr)")).toHaveClass("att-chip-ok");
  });

  it("images show the indexing chip while the note itself is still indexing", () => {
    setup(noteViewFromItem({ ...INDEXED, ingestState: "processing" }));
    fireEvent.click(screen.getByRole("tab", { name: /Attachments/ }));
    expect(screen.getAllByText("indexing…").length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText("ocr queued…")).not.toBeInTheDocument();
  });

  it("⋯ opens the file sheet with an open link; remove needs the tap-again confirm", async () => {
    const { onRemoveAttachment } = setup(noteViewFromItem(INDEXED));
    fireEvent.click(screen.getByRole("tab", { name: /Attachments/ }));
    fireEvent.click(screen.getByRole("button", { name: "Actions for lab-orders.pdf" }));

    const open = screen.getByText("open").closest("a");
    expect(open).toHaveAttribute("href", "/api/attachments/a1");
    expect(open).toHaveAttribute("target", "_blank");

    fireEvent.click(screen.getByRole("button", { name: "remove" }));
    expect(onRemoveAttachment).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "tap again — removes file + its extracted text" }),
    );
    expect(onRemoveAttachment).toHaveBeenCalledWith("a1");
    await waitFor(() => expect(screen.queryByText("lab-orders.pdf")).not.toBeInTheDocument());
  });

  it("add files uploads through the handler and appends a manifest row", async () => {
    const { onAddAttachment } = setup(noteViewFromItem(INDEXED));
    fireEvent.click(screen.getByRole("tab", { name: /Attachments/ }));

    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("file input missing");
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(screen.getByText("notes.txt")).toBeInTheDocument());
    expect(onAddAttachment).toHaveBeenCalledWith("n1", file);
    expect(screen.getByRole("tab", { name: /Attachments/ })).toHaveTextContent("4");
  });

  it("Analysis tab: title, tags, and facts as edges grouped by subject", async () => {
    stubAnalysisFetch(ANALYSIS);
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Analysis" }));

    expect(await screen.findByText("Dr. Patel visit — BP 128/82")).toBeInTheDocument();
    expect(screen.getByText("blood-pressure")).toBeInTheDocument();
    expect(screen.getByText("dr-patel")).toBeInTheDocument();

    // Edge rows: monospace predicate path → value, kind badge, status chrome.
    expect(screen.getByText("blood_pressure")).toBeInTheDocument();
    expect(screen.getByText("128/82 mmHg")).toBeInTheDocument();
    expect(screen.getByText("address.home")).toBeInTheDocument();
    expect(screen.getByText("measurement")).toBeInTheDocument();
    expect(screen.getByText("pending review")).toBeInTheDocument();
    expect(screen.getByText("pinned")).toBeInTheDocument();
    expect(screen.getByText("97%")).toBeInTheDocument();

    // Subject grouping: Me appears once even with two facts; Sarah is
    // provisional and gets the muted chip.
    expect(screen.getAllByRole("button", { name: /Me person/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Sarah person provisional/ })).toBeInTheDocument();

    // Retracted facts never render here — entity history rails own the past.
    expect(screen.queryByText("height")).not.toBeInTheDocument();
    expect(screen.queryByText("retracted")).not.toBeInTheDocument();

    // The Sep-2026 token renders as a calm date chip.
    expect(screen.getByText("“in three months (September)”")).toBeInTheDocument();
    expect(screen.getByText(/analyzed .*xai:grok-4\.3/)).toBeInTheDocument();
  });

  it("tapping a fact expands its citation with the source words highlighted", async () => {
    stubAnalysisFetch(ANALYSIS);
    const { onOpenEntity } = setup();
    fireEvent.click(screen.getByRole("tab", { name: "Analysis" }));

    const row = await screen.findByRole("button", { name: /blood_pressure/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(row);

    expect(screen.getByText("BP 128/82").closest("mark")).toHaveClass("snip-mark");
    expect(screen.getByText(/reported .* · xai:grok-4\.3 .* 97%/)).toBeInTheDocument();
    // No direct edit: the correction affordance routes via review.
    expect(screen.getByText(/fix via review/)).toBeInTheDocument();

    // Subject headers double as entity navigation.
    fireEvent.click(screen.getByRole("button", { name: /Sarah person provisional/ }));
    expect(onOpenEntity).toHaveBeenCalledWith("ent-sarah");
  });

  it("an un-analyzed note shows the quiet line, driven by analyzed_at null", async () => {
    stubAnalysisFetch({
      ...ANALYSIS,
      analyzed_at: null,
      title: null,
      tags: [],
      facts: [],
      entities: [],
      temporal_tokens: [],
    });
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Analysis" }));
    expect(
      await screen.findByText("analysis runs after indexing — nothing here yet."),
    ).toBeInTheDocument();
  });

  it("the top-right ⋯ sheet drives edit / move / delete, with a tap-again delete", () => {
    const { onDelete, onEdit, onMove } = setup();

    fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
    fireEvent.click(screen.getByRole("button", { name: "delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "tap again — deletes this note" }));
    expect(onDelete).toHaveBeenCalledWith("n1");

    fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
    fireEvent.click(screen.getByRole("button", { name: "move domain" }));
    expect(onMove).toHaveBeenCalledWith({ id: "n1", domain: "health", destination: "Labs" });

    fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
    fireEvent.click(screen.getByRole("button", { name: "edit" }));
    expect(onEdit).toHaveBeenCalledWith(
      "n1",
      ITEM.body,
      ITEM.domain,
      ITEM.createdAt,
      ITEM.attachments,
    );
    // Picking an action closes the sheet.
    expect(screen.queryByRole("button", { name: "edit" })).not.toBeInTheDocument();
  });

  it("a search opening starts from the preview, then resolves the full note", async () => {
    const result: SearchResult = {
      note_id: "n1",
      chunk_id: "c1",
      snippet: "snippet",
      match: "both",
      score: 1,
      domain: "health",
      destination: "Labs",
      created_at: ITEM.createdAt.toISOString(),
      body_preview: "first paragraph",
      attachment_count: 1,
      source_kind: "note",
      source_anchor: null,
    };
    const resolve = vi.fn(async () => ITEM);
    setup(noteViewFromSearch(result), resolve);

    expect(screen.getByText("loading the full note…")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("second paragraph")).toBeInTheDocument());
    expect(resolve).toHaveBeenCalledWith("n1");
    expect(screen.queryByText("loading the full note…")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Attachments/ }));
    expect(screen.getByText("lab-orders.pdf")).toBeInTheDocument();
  });
});
