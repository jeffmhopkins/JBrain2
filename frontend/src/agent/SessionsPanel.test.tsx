import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SessionsPanel } from "./SessionsPanel";
import type { AgentSession, SessionCreate } from "./types";

function session(over: Partial<AgentSession>): AgentSession {
  return {
    id: "s1",
    title: "Health wiki cleanup",
    status: "active",
    agent: "curator",
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
  it("lists chats with their read-scope as a tinted dot (calm domain labels)", () => {
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
        onRescope={vi.fn()}
      />,
    );
    expect(screen.getByText("Health wiki cleanup")).toBeInTheDocument();
    expect(screen.getByText("Recap")).toBeInTheDocument();
    // Scope is the row's dot now — its label rides the title attribute.
    expect(screen.getByTitle("reads medical")).toBeInTheDocument();
    expect(screen.getByTitle("reads financial")).toBeInTheDocument();
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByRole("button", { name: "Medical" }));
    fireEvent.change(screen.getByLabelText("Session title"), { target: { value: "labs" } });
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));

    await waitFor(() => expect(onCreate).toHaveBeenCalled());
    expect(onCreate).toHaveBeenCalledWith({
      domain_scopes: ["general", "health"],
      title: "labs",
      agent: "curator",
    });
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(created));
  });

  it("a no-data agent (Jerv) hides the scope dial and starts with empty scopes", async () => {
    const created = session({ id: "j", title: "", domain_scopes: [], agent: "jerv" });
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
        onRescope={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("＋ New chat"));
    // The default curator shows the scope dial.
    expect(screen.getByRole("button", { name: "Everything" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Jerv/ }));
    // Jerv reads no owner data: the scope dial is gone, replaced by the caveat.
    expect(screen.queryByRole("button", { name: "Everything" })).not.toBeInTheDocument();
    expect(screen.getByText(/No access to your notes/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Start/ }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({ domain_scopes: [], title: "", agent: "jerv" }),
    );
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(created));
  });

  it("the Teacher agent hides the scope dial and starts with no data", async () => {
    const created = session({ id: "t", title: "", domain_scopes: [], agent: "teacher" });
    const onCreate = vi.fn(async (_body: SessionCreate) => created);
    render(
      <SessionsPanel
        sessions={[]}
        onOpen={vi.fn()}
        onCreate={onCreate}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
        onRescope={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByRole("button", { name: /Teacher/ }));
    expect(screen.queryByRole("button", { name: "Everything" })).not.toBeInTheDocument();
    expect(screen.getByText(/no access to your notes or data/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({ domain_scopes: [], title: "", agent: "teacher" }),
    );
  });

  it("switching back to Curator restores the scope dial and its selection", () => {
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
        onRescope={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("＋ New chat"));
    // Narrow Curator to Medical, then flip to Jerv (dial hidden) and back.
    fireEvent.click(screen.getByRole("button", { name: "Medical" }));
    fireEvent.click(screen.getByRole("button", { name: /Jerv/ }));
    expect(screen.queryByRole("button", { name: "Medical" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Curator/ }));
    // The dial is back and still on Medical — the scope selection wasn't lost.
    expect(screen.getByRole("button", { name: "Medical" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Start/ })).toHaveTextContent("reads medical");
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
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
        onRescope={vi.fn()}
      />,
    );
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /^archive/ }));
    expect(onArchive).toHaveBeenCalledWith("s1");
  });

  it("keeps archived chats in their own segment with an unarchive rail", () => {
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
        onRescope={vi.fn()}
      />,
    );
    // The archived chat lives in its own segment; the live one shows by default.
    expect(screen.getByText("Live chat")).toBeInTheDocument();
    expect(screen.queryByText("Old chat")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Archived/ }));
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

  it("shows the compact row's turn count and a staged badge", () => {
    render(
      <SessionsPanel
        sessions={[session({ turn_count: 14, staged_count: 1 })]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
        onRescope={vi.fn()}
      />,
    );
    expect(screen.getByText("14 turns")).toBeInTheDocument();
    expect(screen.getByText("1 staged")).toBeInTheDocument();
  });

  it("the rail's scope action re-scopes the chat (and doesn't open it)", () => {
    const onOpen = vi.fn();
    const onRescope = vi.fn();
    render(
      <SessionsPanel
        sessions={[session({ domain_scopes: ["general"] })]}
        onOpen={onOpen}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
        onArchive={vi.fn()}
        onUnarchive={vi.fn()}
        onRescope={onRescope}
      />,
    );
    // Swipe to the rail, then scope opens the sheet — not the chat.
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /scope/ }));
    expect(onOpen).not.toHaveBeenCalled();
    // Widen to Medical and save.
    fireEvent.click(screen.getByRole("button", { name: "Medical" }));
    fireEvent.click(screen.getByRole("button", { name: /Save scope/ }));
    expect(onRescope).toHaveBeenCalledWith("s1", ["general", "health"]);
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
        onRescope={vi.fn()}
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
