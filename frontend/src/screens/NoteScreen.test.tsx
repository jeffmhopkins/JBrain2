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

function setup(
  source = noteViewFromItem(ITEM),
  resolve: (id: string) => Promise<StreamItem | null> = vi.fn(async () => null),
) {
  const handlers = {
    onClose: vi.fn(),
    onEdit: vi.fn(),
    onMove: vi.fn(),
    onDelete: vi.fn(),
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

  it("renders the body as paragraphs and attachment cards that open in a new tab", () => {
    setup();
    expect(screen.getByText("first paragraph")).toBeInTheDocument();
    expect(screen.getByText("second paragraph")).toBeInTheDocument();
    const link = screen.getByText("lab-orders.pdf").closest("a");
    expect(link).toHaveAttribute("href", "/api/attachments/a1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.getByText("24 KB")).toBeInTheDocument();
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
    expect(onEdit).toHaveBeenCalledWith("n1", ITEM.body);
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
    expect(screen.getByText("lab-orders.pdf")).toBeInTheDocument();
  });
});
