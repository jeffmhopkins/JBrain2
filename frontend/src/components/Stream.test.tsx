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
    attachments: [],
    pending: false,
    ...overrides,
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
    onMove: vi.fn(),
    onDelete: vi.fn(),
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

  it("shows the indexing chip for pending/processing and failure for failed", () => {
    renderStream([
      item({ ingestState: "pending" }),
      item({ ingestState: "processing" }),
      item({ ingestState: "failed" }),
      item({ ingestState: "indexed" }),
    ]);
    expect(screen.getAllByText("indexing…")).toHaveLength(2);
    expect(screen.getAllByText("indexing failed")).toHaveLength(1);
  });

  it("opens the note view on a bubble tap", () => {
    const note = item({ body: "tap target" });
    const { onOpenNote } = renderStream([note]);
    fireEvent.click(screen.getByRole("button", { name: /tap target/ }));
    expect(onOpenNote).toHaveBeenCalledWith(note);
  });

  it("swipe-left reveals the rail; Edit and Move dispatch their actions", () => {
    const note = item({ body: "swipe me" });
    const { onEdit, onMove } = renderStream([note]);
    const bubble = screen.getByRole("button", { name: /swipe me/ });

    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    swipeLeft(bubble);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEdit).toHaveBeenCalledWith(note);

    swipeLeft(bubble);
    fireEvent.click(screen.getByRole("button", { name: "Move domain" }));
    expect(onMove).toHaveBeenCalledWith(note);
  });

  it("vertical drags do not open the rail", () => {
    const note = item({ body: "scroll me" });
    renderStream([note]);
    const bubble = screen.getByRole("button", { name: /scroll me/ });
    fireEvent.touchStart(bubble, { touches: [{ clientX: 250, clientY: 50 }] });
    fireEvent.touchMove(bubble, { touches: [{ clientX: 248, clientY: 220 }] });
    fireEvent.touchEnd(bubble);
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("delete requires the inline tap-again confirm", () => {
    const note = item({ body: "doomed note" });
    const { onDelete } = renderStream([note]);
    swipeLeft(screen.getByRole("button", { name: /doomed note/ }));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Tap again" }));
    expect(onDelete).toHaveBeenCalledWith(note.id);
  });

  it("offers no rail on outbox rows that have no server id yet", () => {
    const note = item({ id: null, pending: true, body: "still local" });
    renderStream([note]);
    swipeLeft(screen.getByRole("button", { name: /still local/ }));
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });
});
