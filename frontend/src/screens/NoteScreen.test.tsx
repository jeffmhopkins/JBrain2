import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SearchResult } from "../api/client";
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
    { id: "a1", filename: "lab-orders.pdf", mediaType: "application/pdf", sizeBytes: 24_120 },
  ],
  pending: false,
};

// Indexed variant with a PDF (searchable) and an image (awaits Phase 3 OCR).
const INDEXED: StreamItem = {
  ...ITEM,
  ingestState: "indexed",
  attachments: [
    ...ITEM.attachments,
    { id: "a2", filename: "receipt.png", mediaType: "image/png", sizeBytes: 512_000 },
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
    })),
    onRemoveAttachment: vi.fn(async () => {}),
  };
  render(<NoteScreen source={source} resolve={resolve} syncStatus="synced" {...handlers} />);
  return handlers;
}

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

    expect(
      screen.getByText("2 files · 524 KB · 1 searchable · 1 awaiting ocr (p3)"),
    ).toBeInTheDocument();
    expect(screen.getByText("lab-orders.pdf")).toBeInTheDocument();
    expect(screen.getByText("24 KB · application/pdf")).toBeInTheDocument();
    expect(screen.getByText("text extracted")).toBeInTheDocument();
    expect(screen.getByText("no text layer — ocr in p3")).toBeInTheDocument();
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
    expect(screen.getByRole("tab", { name: /Attachments/ })).toHaveTextContent("3");
  });

  it("Analysis tab shows the phased placeholder sections", () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Analysis" }));
    for (const header of ["Tags", "Salient facts", "Entities"]) {
      expect(screen.getByText(header)).toBeInTheDocument();
    }
    expect(screen.getAllByText("arrives in Phase 3")).toHaveLength(3);
    expect(screen.getByText("Wiki backlinks")).toBeInTheDocument();
    expect(screen.getByText("arrives in Phase 6")).toBeInTheDocument();
    expect(screen.getByText(/extraction provenance/)).toBeInTheDocument();
  });

  it("delete needs the inline confirm; edit hands off id + body", () => {
    const { onDelete, onEdit } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Tap again to confirm" }));
    expect(onDelete).toHaveBeenCalledWith("n1");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEdit).toHaveBeenCalledWith(
      "n1",
      ITEM.body,
      ITEM.domain,
      ITEM.createdAt,
      ITEM.attachments,
    );
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
