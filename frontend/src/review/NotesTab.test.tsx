import { render, screen } from "@testing-library/react";
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
    ask: "Which Sarah?",
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
        onOpenConversation={vi.fn()}
      />,
    );

    expect(screen.getByText(/health/)).toBeInTheDocument();
    expect(screen.getByText(/finance/)).toBeInTheDocument();
    // The approval row names its domain too — it is a change to standing instructions,
    // and which firewall it sits behind is exactly as legible as for a note.
    expect(screen.getByText(/preferences · general/)).toBeInTheDocument();
  });

  it("says so when a domain code is not one it knows, rather than degrading to a dot", () => {
    render(
      <NotesTab rows={[row({ domain: "wat" })]} loadError={false} onOpenConversation={vi.fn()} />,
    );
    expect(screen.getByText(/unknown domain/)).toBeInTheDocument();
  });
});
