import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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

describe("SessionsPanel", () => {
  it("lists sessions with their read-scope pills", () => {
    render(
      <SessionsPanel
        sessions={[
          session({}),
          session({ id: "s2", title: "Recap", domain_scopes: ["general", "finance"] }),
        ]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText("Health wiki cleanup")).toBeInTheDocument();
    expect(screen.getByText("Recap")).toBeInTheDocument();
    expect(screen.getByText("finance")).toBeInTheDocument();
  });

  it("opens a session on tap", () => {
    const onOpen = vi.fn();
    render(
      <SessionsPanel
        sessions={[session({})]}
        onOpen={onOpen}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Health wiki cleanup"));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "s1" }));
  });

  it("creates a least-privilege session and opens it", async () => {
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
      />,
    );

    fireEvent.click(screen.getByText("＋ New session — choose sources"));
    // Default selection is general only (least privilege).
    expect(screen.getByRole("button", { name: /Start session/ })).toHaveTextContent(
      "reads general",
    );

    fireEvent.click(screen.getByRole("button", { name: /Health/ })); // widen to health
    fireEvent.change(screen.getByLabelText("Session title"), { target: { value: "labs" } });
    fireEvent.click(screen.getByRole("button", { name: /Start session/ }));

    await waitFor(() => expect(onCreate).toHaveBeenCalled());
    expect(onCreate).toHaveBeenCalledWith({ domain_scopes: ["general", "health"], title: "labs" });
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(created));
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
      />,
    );
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /delete/ }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /tap again/ }));
    expect(onDelete).toHaveBeenCalledWith("s1");
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
      />,
    );
    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /rename/ }));
    const input = screen.getByLabelText("Session title");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith("s1", "Renamed");
  });

  it("disables Start when no domain is selected", () => {
    render(
      <SessionsPanel
        sessions={[]}
        onOpen={vi.fn()}
        onCreate={vi.fn()}
        onClose={vi.fn()}
        onRename={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("＋ New session — choose sources"));
    fireEvent.click(screen.getByRole("button", { name: /General/ })); // deselect the default
    expect(screen.getByRole("button", { name: /Start session/ })).toBeDisabled();
  });
});
