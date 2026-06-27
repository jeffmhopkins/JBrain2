import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { JcodeModelStatus, JcodeSession } from "../jcode/types";
import { JcodeSessionScreen } from "./JcodeSessionScreen";

// xterm paints to a canvas renderer jsdom lacks, so stub it — the terminal's job here is to
// mount and open the session's shell socket; the byte wiring is unit-tested in
// jcode/terminal.test.ts.
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    loadAddon() {}
    open() {}
    focus() {}
    write() {}
    dispose() {}
    onData() {
      return { dispose() {} };
    }
    onResize() {
      return { dispose() {} };
    }
  },
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {}
  },
}));

// The terminal opens a real WebSocket; jsdom has none. A fake records the URLs it dialed,
// the bytes it sent, and the live instances (so a test can fire `onclose`).
const wsInstances: FakeWS[] = [];
const wsUrls: string[] = [];
const wsSent: Uint8Array[] = [];
class FakeWS {
  binaryType = "blob";
  readyState = 1;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(url: string) {
    wsUrls.push(url);
    wsInstances.push(this);
  }
  send(data: Uint8Array) {
    wsSent.push(data);
  }
  // Real browsers fire `close` when the socket is closed (including on our own unmount);
  // mimic that so the component's `disposed` guard is genuinely exercised.
  close() {
    this.onclose?.();
  }
}

const MODEL_STATUS: JcodeModelStatus = {
  model: "qwen3-coder-next",
  served: "qwen3-coder-next",
  loaded: true,
  warming: false,
  progress: null,
  hosting: true,
  size_gb: 49.6,
  context_window: 262144,
  resident: ["qwen3-coder-next"],
};

beforeEach(() => {
  wsInstances.length = 0;
  wsUrls.length = 0;
  wsSent.length = 0;
  vi.stubGlobal("WebSocket", FakeWS);
  // The screen polls model residency on mount; default to settled (loaded, not warming) so
  // the terminal mounts straight away (each test overrides for its own case).
  vi.spyOn(api, "jcodeModelStatus").mockResolvedValue(MODEL_STATUS);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const SESSION: JcodeSession = {
  id: "j1",
  repo: "github.com/me/scratch-todo",
  branch: "main",
  work_branch: "jcode/spike",
  status: "ready",
  title: "",
  archived: false,
  created_at: "2026-06-25T00:00:00Z",
  last_active_at: "2026-06-25T00:00:00Z",
};

describe("JcodeSessionScreen", () => {
  it("opens the session's shell socket in the (default) Terminal tab", async () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    // The terminal mounts via a dynamic import once the coder is resident, then dials the
    // owner's terminal proxy.
    await waitFor(() =>
      expect(wsUrls).toContain(`ws://${window.location.host}/api/jcode/sessions/j1/terminal`),
    );
  });

  it("shows the model chip with the full served context (256k)", async () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/qwen3-coder-next · 256k · on-box/)).toBeInTheDocument();
  });

  it("shows the loading bar while the coder warms onto the box", async () => {
    // `warming` drives the bar (not `loaded`, which races true mid-load).
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      ...MODEL_STATUS,
      loaded: false,
      warming: true,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/Loading qwen3-coder-next onto the box/i)).toBeInTheDocument();
  });

  it("drives the loading bar off the gateway's real load fraction when reported", async () => {
    // A real progress signal (42% of weights read in) is shown verbatim, not the time guess.
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      ...MODEL_STATUS,
      loaded: false,
      warming: true,
      progress: 0.42,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/Loading qwen3-coder-next onto the box/i)).toBeInTheDocument();
    expect(await screen.findByText("42%")).toBeInTheDocument();
  });

  it("prompts before swapping when the coder isn't on the box, naming what gets evicted", async () => {
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      ...MODEL_STATUS,
      loaded: false,
      warming: false,
      resident: ["gpt-oss-120b"],
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/Load qwen3-coder-next onto the box\?/i)).toBeInTheDocument();
    expect(screen.getByText(/will unload gpt-oss-120b/i)).toBeInTheDocument();
    expect(screen.queryByText(/Loading qwen3-coder-next onto the box/i)).not.toBeInTheDocument();
  });

  it("warms the coder and shows the loading bar when the owner confirms the swap", async () => {
    vi.spyOn(api, "jcodeModelStatus")
      .mockResolvedValueOnce({ ...MODEL_STATUS, loaded: false, warming: false, resident: [] })
      .mockResolvedValue({ ...MODEL_STATUS, loaded: false, warming: true, resident: [] });
    const warm = vi
      .spyOn(api, "jcodeWarmModel")
      .mockResolvedValue({ ...MODEL_STATUS, loaded: false, warming: true, resident: [] });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    fireEvent.click(await screen.findByText("Load model"));
    await waitFor(() => expect(warm).toHaveBeenCalled());
    expect(await screen.findByText(/Loading qwen3-coder-next onto the box/i)).toBeInTheDocument();
  });

  it("sends arrow sequences from the mobile key row", async () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    const up = await screen.findByLabelText("Up");
    fireEvent.click(up);
    await waitFor(() => expect(wsSent.length).toBeGreaterThan(0));
    expect(wsSent[0]).toEqual(new TextEncoder().encode("\x1b[A"));
  });

  it("arms and disarms the Ctrl modifier on the mobile key row", async () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    const ctrl = await screen.findByText("ctrl");
    expect(ctrl).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(ctrl);
    await waitFor(() => expect(ctrl).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(ctrl); // tap again disarms
    await waitFor(() => expect(ctrl).toHaveAttribute("aria-pressed", "false"));
  });

  it("shows the stopped state with Restart when the session is paused", async () => {
    const restart = vi.spyOn(api, "jcodeRestartSession").mockResolvedValue({
      ...SESSION,
      status: "ready",
    });
    render(<JcodeSessionScreen session={{ ...SESSION, status: "stopped" }} onClose={vi.fn()} />);
    expect(await screen.findByText("Session stopped")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Restart session"));
    await waitFor(() => expect(restart).toHaveBeenCalledWith("j1"));
    // After restart the terminal mounts and dials the shell socket.
    await waitFor(() =>
      expect(wsUrls).toContain(`ws://${window.location.host}/api/jcode/sessions/j1/terminal`),
    );
  });

  it("pauses the session when the shell exits (the socket closes while mounted)", async () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    await waitFor(() => expect(wsInstances.length).toBeGreaterThan(0));
    // The control server closes the socket when the shell exits → the screen shows stopped.
    const ws = wsInstances[wsInstances.length - 1];
    ws?.onclose?.();
    expect(await screen.findByText("Session stopped")).toBeInTheDocument();
  });

  it("does not show the stopped state when switching tabs (the disposed guard)", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: true, url: null });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    await waitFor(() => expect(wsInstances.length).toBeGreaterThan(0));
    // Switching to Preview unmounts the terminal — which closes its socket. The `disposed`
    // guard must suppress that close so it does NOT read as a shell exit / session pause.
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    expect(await screen.findByText(/Start your dev server/i)).toBeInTheDocument();
    expect(screen.queryByText("Session stopped")).not.toBeInTheDocument();
  });

  it("stops the session from the actions menu", async () => {
    const stop = vi.spyOn(api, "jcodeStopSession").mockResolvedValue({
      ...SESSION,
      status: "stopped",
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Session actions"));
    fireEvent.click(screen.getByText("Stop session"));
    await waitFor(() => expect(stop).toHaveBeenCalledWith("j1"));
    expect(await screen.findByText("Session stopped")).toBeInTheDocument();
  });

  it("opens a web preview tunnel from the Preview tab", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: true, url: null });
    vi.spyOn(api, "jcodePreviewOpen").mockResolvedValue({
      enabled: true,
      url: "https://demo-x.trycloudflare.com",
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    fireEvent.click(await screen.findByText("Open preview tunnel"));
    expect(await screen.findByText("https://demo-x.trycloudflare.com")).toBeInTheDocument();
  });

  it("shows a disabled state when preview is off", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: false, url: null });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    expect(await screen.findByText(/isn't enabled/i)).toBeInTheDocument();
  });

  it("arms a tap-again confirm before deleting (from the actions menu)", async () => {
    const del = vi.spyOn(api, "jcodeDeleteSession").mockResolvedValue();
    const onClose = vi.fn();
    render(<JcodeSessionScreen session={SESSION} onClose={onClose} />);

    fireEvent.click(screen.getByLabelText("Session actions"));
    fireEvent.click(screen.getByText("Delete"));
    expect(screen.getByText(/Tap again/)).toBeInTheDocument();
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(/Tap again/));
    await waitFor(() => expect(del).toHaveBeenCalledWith("j1"));
    expect(onClose).toHaveBeenCalled();
  });

  it("mints + copies a single-use link from the share manager", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.spyOn(api, "jcodeListShares").mockResolvedValue([]);
    vi.spyOn(api, "jcodeMintShare").mockResolvedValue({
      id: "p1",
      label: "shared link",
      expires_at: null,
      token: "sekret",
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Session actions"));
    fireEvent.click(screen.getByText("Share link…"));
    fireEvent.click(await screen.findByText("Create link"));
    expect(await screen.findByText("Copy link")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Copy link"));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining("/jcode/s/j1#t=sekret")),
    );
    expect(api.jcodeMintShare).toHaveBeenCalledWith("j1", 24);
  });

  it("hides the owner actions menu and skips the model poll in shared mode", () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} shared />);
    expect(screen.queryByLabelText("Session actions")).not.toBeInTheDocument();
    // The owner-only model-status poll is skipped — a share principal would 403 it.
    expect(api.jcodeModelStatus).not.toHaveBeenCalled();
  });
});
