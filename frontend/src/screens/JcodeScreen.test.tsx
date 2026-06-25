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
    created_at: new Date().toISOString(),
    last_active_at: new Date().toISOString(),
    ...over,
  };
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
});
