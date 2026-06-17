import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { StreamItem } from "../notes/useNotes";
import { Stream } from "./Stream";

let seq = 0;
function item(overrides: Partial<StreamItem> = {}): StreamItem {
  seq += 1;
  return {
    key: `k-${seq}`,
    id: `n-${seq}`,
    domain: "general",
    destination: null,
    body: `note body ${seq}`,
    createdAt: new Date(),
    ingestState: "indexed",
    analyzed: true,
    provenance: "human",
    attachments: [],
    pending: false,
    hidden: false,
    ...overrides,
  };
}

function imageAttachment(name: string, hasExtracts = false) {
  // hasDescription rides along; the stream chips only read hasExtracts.
  return {
    id: `att-${name}`,
    filename: name,
    mediaType: "image/png",
    sizeBytes: 1024,
    hasExtracts,
    hasDescription: false,
  };
}

function daysAgo(days: number): Date {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d;
}

function renderStream(items: StreamItem[]) {
  const handlers = {
    onOpenSearch: vi.fn(),
    onOpenNote: vi.fn(),
    onEdit: vi.fn(),
    onDelete: vi.fn(),
    onHide: vi.fn(),
  };
  render(<Stream items={items} {...handlers} />);
  return handlers;
}

function swipeLeft(el: Element) {
  fireEvent.touchStart(el, { touches: [{ clientX: 250, clientY: 50 }] });
  fireEvent.touchMove(el, { touches: [{ clientX: 60, clientY: 52 }] });
  fireEvent.touchEnd(el);
}

describe("Stream", () => {
  it("bounds the stream to the last 2 days and routes older notes to Search", () => {
    const old = item({ body: "ancient note", createdAt: daysAgo(8) });
    const fresh = item({ body: "fresh note", createdAt: daysAgo(0) });
    const { onOpenSearch } = renderStream([old, fresh]);

    expect(screen.getByText("fresh note")).toBeInTheDocument();
    expect(screen.queryByText("ancient note")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /older notes live in Search/ }));
    expect(onOpenSearch).toHaveBeenCalled();
  });

  it("clamps bubble bodies at 3 lines", () => {
    renderStream([item({ body: "clamp me" })]);
    expect(screen.getByText("clamp me")).toHaveClass("note-body-clamp");
  });

  it("tags agent-authored notes and leaves human ones untagged", () => {
    renderStream([
      item({ body: "agent note", provenance: "agent" }),
      item({ body: "my note", provenance: "human" }),
    ]);
    // The tag appears once — only on the agent note (attribution is metadata,
    // not body text, so "agent note" itself never contains it).
    expect(screen.getAllByText(/assistant/i)).toHaveLength(1);
  });

  it("walks the chip through the pipeline lifecycle, one state per bubble", () => {
    renderStream([
      item({ ingestState: "pending" }),
      item({ ingestState: "processing" }),
      item({ ingestState: "failed" }),
      item({
        ingestState: "indexed",
        analyzed: false,
        attachments: [imageAttachment("receipt.png")],
      }),
      item({
        ingestState: "indexed",
        analyzed: false,
        attachments: [imageAttachment("a.png"), imageAttachment("b.png")],
      }),
      item({ ingestState: "indexed", analyzed: false }),
      item({ ingestState: "indexed", analyzed: true }),
    ]);
    expect(screen.getAllByText("indexing…")).toHaveLength(2);
    expect(screen.getAllByText("indexing failed")).toHaveLength(1);
    expect(screen.getAllByText("reading image…")).toHaveLength(1);
    expect(screen.getAllByText("reading images…")).toHaveLength(1);
    expect(screen.getAllByText("analyzing…")).toHaveLength(1);
    // Six chips total: the analyzed bubble ends the lifecycle chip-free.
    expect(document.querySelectorAll(".chip-pending, .chip-failed")).toHaveLength(6);
  });

  it("an image whose OCR is cached doesn't reopen the chip", () => {
    renderStream([
      item({
        ingestState: "indexed",
        analyzed: true,
        attachments: [imageAttachment("done.png", true)],
      }),
    ]);
    expect(screen.queryByText("reading image…")).not.toBeInTheDocument();
    expect(screen.queryByText("analyzing…")).not.toBeInTheDocument();
  });

  it("opens the note view on a bubble tap", () => {
    const note = item({ body: "tap target" });
    const { onOpenNote } = renderStream([note]);
    fireEvent.click(screen.getByRole("button", { name: /tap target/ }));
    expect(onOpenNote).toHaveBeenCalledWith(note);
  });

  it("swipe-left reveals the rail; Edit and Hide dispatch their actions", () => {
    const note = item({ body: "swipe me" });
    const { onEdit, onHide } = renderStream([note]);
    const bubble = screen.getByRole("button", { name: /swipe me/ });

    expect(screen.queryByRole("button", { name: "edit" })).not.toBeInTheDocument();
    swipeLeft(bubble);
    fireEvent.click(screen.getByRole("button", { name: "edit" }));
    expect(onEdit).toHaveBeenCalledWith(note);

    swipeLeft(bubble);
    fireEvent.click(screen.getByRole("button", { name: "hide" }));
    expect(onHide).toHaveBeenCalledWith(note);
  });

  it("vertical drags do not open the rail", () => {
    const note = item({ body: "scroll me" });
    renderStream([note]);
    const bubble = screen.getByRole("button", { name: /scroll me/ });
    fireEvent.touchStart(bubble, { touches: [{ clientX: 250, clientY: 50 }] });
    fireEvent.touchMove(bubble, { touches: [{ clientX: 248, clientY: 220 }] });
    fireEvent.touchEnd(bubble);
    expect(screen.queryByRole("button", { name: "delete" })).not.toBeInTheDocument();
  });

  it("delete requires the inline tap-again confirm", () => {
    const note = item({ body: "doomed note" });
    const { onDelete } = renderStream([note]);
    swipeLeft(screen.getByRole("button", { name: /doomed note/ }));

    fireEvent.click(screen.getByRole("button", { name: "delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "tap again" }));
    expect(onDelete).toHaveBeenCalledWith(note.id);
  });

  it("offers no rail on outbox rows that have no server id yet", () => {
    const note = item({ id: null, pending: true, body: "still local" });
    renderStream([note]);
    swipeLeft(screen.getByRole("button", { name: /still local/ }));
    expect(screen.queryByRole("button", { name: "edit" })).not.toBeInTheDocument();
  });
});
