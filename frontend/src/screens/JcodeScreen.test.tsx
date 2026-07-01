import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { JcodePowerStatus, JcodeSession } from "../jcode/types";
import { JcodeScreen } from "./JcodeScreen";

function powerStatus(over: Partial<JcodePowerStatus> = {}): JcodePowerStatus {
  return {
    on: true,
    provisioned: true,
    services: [
      { name: "local-llm", running: true },
      { name: "claude-shim", running: true },
      { name: "jcode", running: true },
    ],
    coder_loaded: true,
    warming: false,
    model: "qwen3-coder-next",
    size_gb: 49.6,
    hosting: true,
    live_sessions: 0,
    ...over,
  };
}

// The launcher stacks the session screen over itself; stub it so this launcher unit test
// doesn't drag in the terminal's xterm/WebSocket machinery (covered in its own test).
vi.mock("./JcodeSessionScreen", () => ({
  JcodeSessionScreen: ({ session }: { session: JcodeSession }) => <div>opened {session.repo}</div>,
}));

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
    // The session screen is now stacked over the list (the stubbed screen shows the repo).
    expect(await screen.findByText(/opened github.com\/me\/new/)).toBeInTheDocument();
  });

  it("mints an external endpoint and opens its screen with the one-time secret", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([]);
    vi.spyOn(api, "externalSessions").mockResolvedValue([]);
    const mint = vi.spyOn(api, "externalMint").mockResolvedValue({
      id: "ext-9",
      label: "Laptop",
      expires_at: null,
      token: "sk-ext-secret",
      url: "https://box.example/api/ext/llm/ext-9",
    });
    render(<JcodeScreen onClose={vi.fn()} />);

    fireEvent.click(await screen.findByText("New session"));
    fireEvent.click(screen.getByRole("tab", { name: "External" }));
    fireEvent.change(screen.getByPlaceholderText(/label/i), { target: { value: "Laptop" } });
    fireEvent.click(screen.getByText("Start session →"));

    await waitFor(() => expect(mint).toHaveBeenCalledWith("Laptop"));
    // The endpoint screen shows, with the secret revealed exactly once.
    expect(await screen.findByDisplayValue("sk-ext-secret")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://box.example/api/ext/llm/ext-9")).toBeInTheDocument();
  });

  it("lists existing external endpoints and opens one (no secret)", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([]);
    vi.spyOn(api, "externalSessions").mockResolvedValue([
      {
        id: "ext-1",
        label: "Remote",
        enabled: true,
        created_at: new Date().toISOString(),
        expires_at: null,
        last_used_at: null,
        in_tokens: 5,
        out_tokens: 7,
        requests: 1,
      },
    ]);
    render(<JcodeScreen onClose={vi.fn()} />);

    fireEvent.click(await screen.findByText("Remote"));
    // The endpoint screen shows usage but no secret (it's never re-readable).
    expect(await screen.findByText("Token usage")).toBeInTheDocument();
    expect(screen.queryByLabelText("Access token")).not.toBeInTheDocument();
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

  it("shows the power switch reflecting the on state", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([session()]);
    vi.spyOn(api, "externalSessions").mockResolvedValue([]);
    vi.spyOn(api, "jcodePower").mockResolvedValue(powerStatus({ on: true }));
    render(<JcodeScreen onClose={vi.fn()} />);
    const sw = await screen.findByRole("switch", { name: /code mode power/i });
    expect(sw).toHaveAttribute("aria-checked", "true");
  });

  it("renders the powered-off panel (not an error) when the services are down", async () => {
    // Off → the control server is down, so the session list can't load (non-404 error).
    vi.spyOn(api, "jcodeSessions").mockRejectedValue(new ApiError(502, "down"));
    vi.spyOn(api, "externalSessions").mockResolvedValue([]);
    vi.spyOn(api, "jcodePower").mockResolvedValue(powerStatus({ on: false }));
    render(<JcodeScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/Code mode is off/i)).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't reach code mode/i)).not.toBeInTheDocument();
  });

  it("toggling on opens the bring-up modal and starts the services", async () => {
    vi.spyOn(api, "jcodeSessions").mockRejectedValue(new ApiError(502, "down"));
    vi.spyOn(api, "externalSessions").mockResolvedValue([]);
    vi.spyOn(api, "jcodePower").mockResolvedValue(powerStatus({ on: false }));
    const set = vi.spyOn(api, "jcodeSetPower").mockResolvedValue(powerStatus({ on: true }));
    vi.spyOn(api, "jcodeWarmModel").mockResolvedValue({
      model: "qwen3-coder-next",
      served: "qwen3-coder-next",
      loaded: false,
      warming: true,
      progress: 0,
      hosting: true,
      size_gb: 49.6,
      context_window: 262144,
      resident: [],
    });
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      model: "qwen3-coder-next",
      served: "qwen3-coder-next",
      loaded: true,
      warming: false,
      progress: 1,
      hosting: true,
      size_gb: 49.6,
      context_window: 262144,
      resident: ["qwen3-coder-next"],
    });
    render(<JcodeScreen onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /power on/i }));
    expect(await screen.findByRole("dialog", { name: /powering on/i })).toBeInTheDocument();
    await waitFor(() => expect(set).toHaveBeenCalledWith(true));
  });

  it("powering off with live sessions asks to confirm before stopping", async () => {
    vi.spyOn(api, "jcodeSessions").mockResolvedValue([session()]);
    vi.spyOn(api, "externalSessions").mockResolvedValue([]);
    vi.spyOn(api, "jcodePower").mockResolvedValue(powerStatus({ on: true, live_sessions: 2 }));
    const set = vi.spyOn(api, "jcodeSetPower").mockResolvedValue(powerStatus({ on: false }));
    render(<JcodeScreen onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("switch", { name: /code mode power/i }));
    // The confirm gate holds before anything is stopped.
    expect(await screen.findByText(/2 sessions still running/i)).toBeInTheDocument();
    expect(set).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^power off/i }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(false));
  });
});
