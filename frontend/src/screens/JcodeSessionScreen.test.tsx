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
// Records fit() calls so a test can assert the terminal refits on a panel resize (the
// desktop share case, where the window never resizes after load).
const fitCalls = vi.hoisted(() => ({ count: 0 }));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {
      fitCalls.count++;
    }
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
  fitCalls.count = 0;
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

  it("does not show the stopped state when switching tabs", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: true, url: null });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    await waitFor(() => expect(wsInstances.length).toBeGreaterThan(0));
    // Switching to Preview hides the terminal but keeps it mounted, so its socket stays open
    // and nothing reads as a shell exit / session pause.
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    expect(await screen.findByText(/Start your dev server/i)).toBeInTheDocument();
    expect(screen.queryByText("Session stopped")).not.toBeInTheDocument();
  });

  it("keeps the same shell when flipping to Preview and back (no new socket)", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: true, url: null });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    await waitFor(() => expect(wsInstances.length).toBe(1));
    // Flip to Preview, then back to Terminal. The terminal panel is hidden (not unmounted), so
    // its WebSocket — and the shell behind it — must persist; reconnecting would spawn a fresh
    // bash and lose the session. So no second socket is dialed.
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    await screen.findByText(/Start your dev server/i);
    fireEvent.click(screen.getByRole("tab", { name: "Terminal" }));
    await waitFor(() =>
      expect(screen.queryByText(/Start your dev server/i)).not.toBeInTheDocument(),
    );
    expect(wsInstances.length).toBe(1);
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

  it("opens a web preview tunnel and embeds it as an iframe in the Preview tab", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: true, url: null });
    vi.spyOn(api, "jcodePreviewOpen").mockResolvedValue({
      enabled: true,
      url: "https://demo-x.trycloudflare.com",
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    fireEvent.click(await screen.findByText("Open preview tunnel"));
    const frame = await screen.findByTitle("Dev server preview");
    expect(frame).toHaveAttribute("src", "https://demo-x.trycloudflare.com");
  });

  it("copies the preview address from the actions menu once a tunnel is live", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({
      enabled: true,
      url: "https://demo-x.trycloudflare.com",
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    // The menu only offers preview actions after the Preview tab has loaded a live tunnel.
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    await screen.findByTitle("Dev server preview");
    fireEvent.click(screen.getByLabelText("Session actions"));
    fireEvent.click(screen.getByText("Copy preview address"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://demo-x.trycloudflare.com"));
  });

  it("stops the preview from the actions menu", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({
      enabled: true,
      url: "https://demo-x.trycloudflare.com",
    });
    const close = vi.spyOn(api, "jcodePreviewClose").mockResolvedValue();
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    await screen.findByTitle("Dev server preview");
    fireEvent.click(screen.getByLabelText("Session actions"));
    fireEvent.click(screen.getByText("Stop preview"));
    await waitFor(() => expect(close).toHaveBeenCalledWith("j1"));
    // The tunnel is gone → the iframe is replaced by the start-a-tunnel empty state.
    expect(await screen.findByText(/Start your dev server/i)).toBeInTheDocument();
  });

  it("shows the host-mode preview address + port hint and no tunnel wording", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({
      enabled: true,
      url: null,
      mode: "host",
      port: 5174,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    // Host mode reuses the tab but drops the "tunnel" framing: its own address, run the
    // dev server on the reserved port — no "Open preview tunnel".
    expect(await screen.findByText(/own preview address/i)).toBeInTheDocument();
    expect(screen.getByText(":5174")).toBeInTheDocument();
    expect(screen.getByText("Open preview")).toBeInTheDocument();
    expect(screen.queryByText("Open preview tunnel")).not.toBeInTheDocument();
  });

  it("omits Stop preview from the menu in host mode (no tunnel to stop)", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({
      enabled: true,
      url: "https://abc123-preview.box.test",
      mode: "host",
      port: 5174,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    await screen.findByTitle("Dev server preview");
    fireEvent.click(screen.getByLabelText("Session actions"));
    // Copy stays; Stop is tunnel-only.
    expect(screen.getByText("Copy preview address")).toBeInTheDocument();
    expect(screen.queryByText("Stop preview")).not.toBeInTheDocument();
  });

  it("shows a disabled state when preview is off", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: false, url: null });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    expect(await screen.findByText(/turned off/i)).toBeInTheDocument();
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

  it("fills the window in shared mode (no 430px mobile cap) but not for the owner", async () => {
    const wsUrl = `ws://${window.location.host}/api/jcode/sessions/j1/terminal`;
    // A share-link recipient (often on desktop) gets the full-width terminal; the owner's
    // phone-first screen keeps the centered mobile column.
    const shared = render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} shared />);
    expect(shared.container.querySelector(".jcode-screen")).toHaveClass("jcode-screen--wide");
    // Let the terminal's dynamic import settle before unmount: an import resolving after
    // teardown bypasses the mock and loads the real addon-fit (ReferenceError: self).
    await waitFor(() => expect(wsUrls).toContain(wsUrl));
    shared.unmount();

    const owner = render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(owner.container.querySelector(".jcode-screen")).not.toHaveClass("jcode-screen--wide");
    await waitFor(() => expect(wsUrls.length).toBeGreaterThan(1));
  });

  it("refits the terminal when its panel resizes, not only on window resize", async () => {
    // On desktop the window never resizes after load, so the terminal must refit off its
    // host's actual size to fill the wide share panel. It observes the host with a
    // ResizeObserver; firing that observer must re-run the fit.
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} shared />);
    await waitFor(() =>
      expect(wsUrls).toContain(`ws://${window.location.host}/api/jcode/sessions/j1/terminal`),
    );
    const before = fitCalls.count;
    // The MockResizeObserver (test/setup.ts) records instances and exposes trigger().
    const observers = (globalThis.ResizeObserver as unknown as { instances: { trigger(): void }[] })
      .instances;
    const observer = observers.at(-1);
    expect(observer).toBeDefined();
    observer?.trigger();
    expect(fitCalls.count).toBeGreaterThan(before);
  });
});
