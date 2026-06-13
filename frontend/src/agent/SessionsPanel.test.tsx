import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SessionsPanel } from "./SessionsPanel";
import type { AgentSession, SessionCreate } from "./types";

function session(over: Partial<AgentSession>): AgentSession {
  return {
    id: "s1",
    title: "Health wiki cleanup",
    status: "active",
    domain_scopes: ["health"],
    subject_ids: [],
    created_at: "2026-06-12T00:00:00Z",
    last_active_at: "2026-06-12T00:00:00Z",
    ...over,
  };
}

// Each test starts with no remembered scope, so the default seed is "everything".
beforeEach(() => localStorage.clear());

describe("SessionsPanel", () => {
  it("lists chats with their read-scope as a calm chip (calm domain labels)", () => {
    render(
      <SessionsPanel
        sessions={[
          session({}), // health → "medical"
          session({ id: "s2", title: "Recap", domain_scopes: ["general", "finance"] }),
        ]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    expect(screen.getByText("Health wiki cleanup")).toBeInTheDocument();
    expect(screen.getByText("Recap")).toBeInTheDocument();
    expect(screen.getByText("reads medical")).toBeInTheDocument();
    expect(screen.getByText("reads financial")).toBeInTheDocument();
  });

  it("opens a chat on tap", () => {
    const onOpen = vi.fn();
    render(
      <SessionsPanel
        sessions={[session({})]}
        onOpen={onOpen}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Health wiki cleanup"));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "s1" }));
  });

  it("marks the open chat as current", () => {
    render(
      <SessionsPanel
        sessions={[session({})]}
        activeId="s1"
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Health wiki cleanup/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("defaults a new chat to everything (one-tap start)", () => {
    render(
      <SessionsPanel
        sessions={[]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("＋ New chat"));
    expect(screen.getByRole("button", { name: /Start/ })).toHaveTextContent("reads everything");
  });

  it("a preset narrows the scope without touching the grid", () => {
    render(
      <SessionsPanel
        sessions={[]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByRole("button", { name: "Medical" })); // preset pill
    expect(screen.getByRole("button", { name: /Start/ })).toHaveTextContent("reads medical");
    // The per-domain grid stays hidden until Custom is asked for.
    expect(screen.queryByRole("button", { name: /labs, meds/ })).not.toBeInTheDocument();
  });

  it("creates a chat from a preset and opens it", async () => {
    const created = session({ id: "new", title: "labs", domain_scopes: ["general", "health"] });
    const onCreate = vi.fn(async (_body: SessionCreate) => created);
    const onOpen = vi.fn();
    render(
      <SessionsPanel
        sessions={[]}
        onOpen={onOpen}
        onCreate={onCreate}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByRole("button", { name: "Medical" }));
    fireEvent.change(screen.getByLabelText("Session title"), { target: { value: "labs" } });
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));

    await waitFor(() => expect(onCreate).toHaveBeenCalled());
    expect(onCreate).toHaveBeenCalledWith({ domain_scopes: ["general", "health"], title: "labs" });
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(created));
  });

  it("Custom… reveals the per-domain grid and Start disables when nothing is picked", () => {
    render(
      <SessionsPanel
        sessions={[]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByRole("button", { name: "Medical" })); // general + health
    fireEvent.click(screen.getByRole("button", { name: "Custom…" }));
    // The grid continues from Medical: general + health checked.
    const generalOpt = screen.getByRole("button", { name: /notes, lists, wiki/ });
    const medicalOpt = screen.getByRole("button", { name: /labs, meds/ });
    expect(generalOpt).toHaveAttribute("aria-pressed", "true");
    expect(medicalOpt).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Start/ })).toHaveTextContent("reads medical");
    // Deselect every source → Start can't fire against nothing.
    fireEvent.click(generalOpt);
    fireEvent.click(medicalOpt);
    expect(screen.getByRole("button", { name: /Start/ })).toBeDisabled();
  });

  it("shows the search field only once chats pile up", () => {
    const many = Array.from({ length: 7 }, (_, i) => session({ id: `s${i}`, title: `Chat ${i}` }));
    const { rerender } = render(
      <SessionsPanel
        sessions={many}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("Search chats")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search chats"), { target: { value: "Chat 3" } });
    expect(screen.getByText("Chat 3")).toBeInTheDocument();
    expect(screen.queryByText("Chat 4")).not.toBeInTheDocument();

    rerender(
      <SessionsPanel
        sessions={[session({})]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Search chats")).not.toBeInTheDocument();
  });

  function swipeOpen(): void {
    const slide = document.querySelector(".session-slide") as HTMLElement;
    fireEvent.touchStart(slide, { touches: [{ clientX: 200, clientY: 50 }] });
    fireEvent.touchMove(slide, { touches: [{ clientX: 60, clientY: 52 }] });
    fireEvent.touchEnd(slide);
  }

  it("swipe-left reveals the rail and a tap-again delete fires onDelete", () => {
    const onDelete = vi.fn();
    render(
      <SessionsPanel
        sessions={[session({})]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={onDelete}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /delete/ }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /tap again/ }));
    expect(onDelete).toHaveBeenCalledWith("s1");
  });

  it("swipe-left → archive fires onArchive", () => {
    const onArchive = vi.fn();
    render(
      <SessionsPanel
        sessions={[session({})]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={onArchive}
        onUnarchive={vi.fn()}
      />,
    );
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /^archive/ }));
    expect(onArchive).toHaveBeenCalledWith("s1");
  });

  it("hides archived chats behind a toggle that reveals them and offers unarchive", () => {
    const onUnarchive = vi.fn();
    render(
      <SessionsPanel
        sessions={[
          session({ id: "live1", title: "Live chat" }),
          session({ id: "arch1", title: "Old chat", status: "archived" }),
        ]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={onUnarchive}
      />,
    );
    // The archived chat is tucked away; the live one shows.
    expect(screen.getByText("Live chat")).toBeInTheDocument();
    expect(screen.queryByText("Old chat")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /1 archived/ }));
    expect(screen.getByText("Old chat")).toBeInTheDocument();

    // Its rail offers unarchive, not archive.
    const archivedRow = screen.getByText("Old chat").closest(".session-wrap") as HTMLElement;
    const slide = archivedRow.querySelector(".session-slide") as HTMLElement;
    fireEvent.touchStart(slide, { touches: [{ clientX: 200, clientY: 50 }] });
    fireEvent.touchMove(slide, { touches: [{ clientX: 60, clientY: 52 }] });
    fireEvent.touchEnd(slide);
    fireEvent.click(screen.getByRole("button", { name: /unarchive/ }));
    expect(onUnarchive).toHaveBeenCalledWith("arch1");
  });

  it("swipe-left → rename edits the title inline and fires onRename", () => {
    const onRename = vi.fn();
    render(
      <SessionsPanel
        sessions={[session({})]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={onRename}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
      />,
    );
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /rename/ }));
    const input = screen.getByLabelText("Session title");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith("s1", "Renamed");
  });
});
