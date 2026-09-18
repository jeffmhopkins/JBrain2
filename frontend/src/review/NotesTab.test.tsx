import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { NotesInboxRow } from "../api/client";
import { NotesTab } from "./NotesTab";

function row(over: Partial<NotesInboxRow> = {}): NotesInboxRow {
  return {
    kind: "question",
    session_id: "s1",
    agent: "note_ingest",
    note_id: "n1",
    domain: "general",
    quote: "Ran the 10k with Sarah.",
    asks: ["Which Sarah?"],
    captured_at: "2026-09-01T10:00:00Z",
    waiting_since: "2026-09-01T10:00:00Z",
    committed: 2,
    live: false,
    ...over,
  };
}

describe("the notes tab row", () => {
  it("names each domain in words, not by colour alone", () => {
    // `DomainDot` is a coloured span with a `title` tooltip, and a tooltip does not
    // exist on touch — so this list told a HEALTH note apart from a finance one by hue.
    // D3 calls "never colour alone" a rule for the write rung; it is the same rule here
    // for the same reason, and `EntityWrites` already honours it.
    render(
      <NotesTab
        rows={[
          row({ domain: "health", session_id: "s-h" }),
          row({ domain: "finance", session_id: "s-f" }),
          row({ kind: "approval", domain: "general", session_id: "s-g", captured_at: null }),
        ]}
        loadError={false}
        onOpenRow={vi.fn()}
      />,
    );

    expect(screen.getByText(/health/)).toBeInTheDocument();
    expect(screen.getByText(/finance/)).toBeInTheDocument();
    // The approval row names its domain too — it is a change to standing instructions,
    // and which firewall it sits behind is exactly as legible as for a note.
    expect(screen.getByText(/preferences · general/)).toBeInTheDocument();
  });

  it("quotes the first of a question SET and counts the rest", () => {
    // R1c: one ask carries several questions. The row says how much is waiting without
    // growing to fit — the set itself is one tap away in the thread, which is the only
    // place it can be answered (D4).
    render(
      <NotesTab
        rows={[row({ asks: ["Which Sarah?", "Which coach?", "Which dose?"] })]}
        loadError={false}
        onOpenRow={vi.fn()}
      />,
    );

    expect(screen.getByText(/Which Sarah\?/)).toBeInTheDocument();
    expect(screen.getByText("+2 more")).toBeInTheDocument();
    expect(screen.queryByText(/Which dose\?/)).not.toBeInTheDocument();
  });

  // ⟲ A notes row used to hand off to home's conversation surface. The owner reversed
  // that on 2026-09-14 — "it shouldn't open in the brain chat. It should open up right
  // there in the note entry chat" — and his ruling is about every door into a note
  // conversation, not only the stream's chip. A row about NO note is the exception, and
  // it is the only one.
  it("hands back the row, note and all, so a note row can open its note", () => {
    const onOpenRow = vi.fn();
    render(
      <NotesTab
        rows={[row(), row({ kind: "approval", note_id: null, session_id: "s2" })]}
        loadError={false}
        onOpenRow={onOpenRow}
      />,
    );
    const rows = screen.getAllByRole("button");
    fireEvent.click(rows[0] as HTMLElement);
    expect(onOpenRow.mock.calls[0]?.[0]).toMatchObject({ note_id: "n1" });
    fireEvent.click(rows[1] as HTMLElement);
    expect(onOpenRow.mock.calls[1]?.[0]).toMatchObject({ note_id: null, session_id: "s2" });
  });

  it("says so when a domain code is not one it knows, rather than degrading to a dot", () => {
    render(<NotesTab rows={[row({ domain: "wat" })]} loadError={false} onOpenRow={vi.fn()} />);
    expect(screen.getByText(/unknown domain/)).toBeInTheDocument();
  });
});
