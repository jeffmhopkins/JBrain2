import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { JcodeSession } from "../jcode/types";
import { JcodeScreen } from "./JcodeScreen";

function session(over: Partial<JcodeSession> = {}): JcodeSession {
  return {
    id: "j1",
    repo: "github.com/me/scratch-todo",
    branch: "main",
    work_branch: "jcode/spike",
    status: "ready",
    title: "",
    archived: false,
    created_at: new Date().toISOString(),
    last_active_at: new Date().toISOString(),
    ...over,
  };
}

function swipeOpen(): void {
  const slide = document.querySelector(".jcode-slide") as HTMLElement;
  fireEvent.touchStart(slide, { touches: [{ clientX: 200, clientY: 50 }] });
  fireEvent.touchMove(slide, { touches: [{ clientX: 60, clientY: 52 }] });
  fireEvent.touchEnd(slide);
}

describe("JcodeScreen (launcher)", () => {
  it("lists today's sessions", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([session()]);
    render(<JcodeScreen onClose={vi.fn()} />);
    expect(await screen.findByText("github.com/me/scratch-todo")).toBeInTheDocument();
  });

  it("shows a disabled state when code mode is off (404)", async () => {
    vi.spyOn(api, "jcodeSessions").mockRejectedValue(new ApiError(404, "not enabled"));
    render(<JcodeScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/isn't enabled/i)).toBeInTheDocument();
  });

  it("creates a session from the new-session sheet and opens it", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([]);
    const create = vi
      .spyOn(api, "jcodeCreateSession")
      .mockResolvedValue(session({ id: "j9", repo: "github.com/me/new" }));
    render(<JcodeScreen onClose={vi.fn()} />);

    fireEvent.click(await screen.findByText("New session"));
    fireEvent.change(screen.getByPlaceholderText(/github.com/i), {
      target: { value: "https://github.com/me/new" },
    });
    fireEvent.click(screen.getByText("Start session →"));

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        repo: "https://github.com/me/new",
        branch: "main",
        work_branch: "",
      }),
    );
    // The session screen is now stacked over the list (its composer prompt shows).
    expect(await screen.findByText(/Tell jcode what to build/i)).toBeInTheDocument();
  });

  it("swipe-left → tap-again delete fires jcodeDeleteSession", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([session()]);
    const del = vi.spyOn(api, "jcodeDeleteSession").mockResolvedValue();
    render(<JcodeScreen onClose={vi.fn()} />);
    await screen.findByText("github.com/me/scratch-todo");

    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /delete/ }));
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /tap again/ }));
    expect(del).toHaveBeenCalledWith("j1");
  });

  it("swipe-left → archive fires jcodeArchiveSession", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([session()]);
    const archive = vi.spyOn(api, "jcodeArchiveSession").mockResolvedValue();
    render(<JcodeScreen onClose={vi.fn()} />);
    await screen.findByText("github.com/me/scratch-todo");

    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /^archive/ }));
    expect(archive).toHaveBeenCalledWith("j1");
  });

  it("keeps archived sessions in their own bucket with an unarchive rail", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([
      session({ id: "live1", repo: "github.com/me/live" }),
      session({ id: "arch1", repo: "github.com/me/old", archived: true }),
    ]);
    const unarchive = vi.spyOn(api, "jcodeUnarchiveSession").mockResolvedValue();
    render(<JcodeScreen onClose={vi.fn()} />);

    // The live one shows by default; the archived one is hidden until its tab.
    expect(await screen.findByText("github.com/me/live")).toBeInTheDocument();
    expect(screen.queryByText("github.com/me/old")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Archived/ }));
    expect(screen.getByText("github.com/me/old")).toBeInTheDocument();

    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /unarchive/ }));
    expect(unarchive).toHaveBeenCalledWith("arch1");
  });

  it("swipe-left → rename submits the new title", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([session()]);
    const rename = vi.spyOn(api, "jcodeRenameSession").mockResolvedValue();
    render(<JcodeScreen onClose={vi.fn()} />);
    await screen.findByText("github.com/me/scratch-todo");

    swipeOpen();
    fireEvent.click(screen.getByRole("button", { name: /rename/ }));
    const input = screen.getByLabelText("Session title");
    fireEvent.change(input, { target: { value: "todo spike" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(rename).toHaveBeenCalledWith("j1", "todo spike");
  });
});
