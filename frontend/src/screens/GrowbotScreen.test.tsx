import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GrowbotFlashJob, GrowbotRobot, GrowbotStatus } from "../api/client";
import { CERT_NOTED_KEY } from "../growbot";
import { GrowbotScreen } from "./GrowbotScreen";

// The screen the owner opens every day: robots first, Open GrowBot the biggest button, and the
// key actions saying what they cost (binding mock docs/mocks/growbot-flash/b-growbot-screen.html).

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const NOW = Date.now();

function robot(over: Partial<GrowbotRobot> = {}): GrowbotRobot {
  return {
    code: "gb-3fa91c",
    name: "Quorra",
    created_at: new Date(NOW - 7 * 864e5).toISOString(),
    rotated_at: null,
    revoked_at: null,
    last_seen_at: new Date(NOW - 20_000).toISOString(),
    connected: true,
    ...over,
  };
}

const TAMSIN = robot({
  code: "gb-91d0e4",
  name: "Tamsin",
  connected: false,
  last_seen_at: new Date(NOW - 3 * 36e5).toISOString(),
});

function job(over: Partial<GrowbotFlashJob> = {}): GrowbotFlashJob {
  return {
    id: "job-1",
    mode: "rekey",
    state: "running",
    name: "Quorra",
    code: "gb-3fa91c",
    port: "/dev/ttyACM0",
    box_host: "192.168.1.20",
    phases: { load: "skip", copy: "run", join: "wait" },
    log: ["mpremote: writing the new key"],
    fail: null,
    started_at: new Date(NOW).toISOString(),
    finished_at: null,
    ...over,
  };
}

type Route = (path: string, init?: RequestInit) => Response | null;

function serve(status: GrowbotStatus, extra?: Route, flash: GrowbotFlashJob | null = null) {
  const calls: { path: string; method: string; body?: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const custom = extra?.(path, init);
    if (custom) return custom;
    if (path === "/api/growbot/status") return json(status);
    if (path === "/api/growbot/flash" && method === "GET") return json({ job: flash });
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

describe("GrowbotScreen", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("puts each robot first, with its state in words and Open GrowBot as its button", async () => {
    serve({ service: "up", connected: 1, robots: [robot(), TAMSIN] });
    render(<GrowbotScreen />);
    const quorra = await screen.findByRole("region", { name: "Quorra" });
    expect(within(quorra).getByText(/^connected · just now/)).toBeInTheDocument();
    expect(within(quorra).getByRole("button", { name: "Open GrowBot" })).toBeInTheDocument();
    expect(within(quorra).getByText("gb-3fa91c")).toBeInTheDocument();
    expect(within(quorra).getByText(/^made .*, only on this robot$/)).toBeInTheDocument();
    const tamsin = screen.getByRole("region", { name: "Tamsin" });
    expect(within(tamsin).getByText("not connected · last seen 3 h ago")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a robot" })).toBeInTheDocument();
  });

  it("offers a revoked robot a reflash instead of an open, and no key actions", async () => {
    serve({
      service: "up",
      connected: 0,
      robots: [robot({ revoked_at: new Date(NOW).toISOString(), connected: false })],
    });
    render(<GrowbotScreen />);
    const card = await screen.findByRole("region", { name: "Quorra" });
    expect(within(card).getByText("key revoked — can't connect")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Open GrowBot" })).toBeNull();
    expect(within(card).queryByRole("button", { name: "Revoke key" })).toBeNull();
    expect(within(card).getByRole("button", { name: "Reflash Quorra" })).toBeInTheDocument();
  });

  it("says growbot is down as what it costs, on the screen and on every robot", async () => {
    serve({ service: "down", connected: 0, robots: [robot()] });
    render(<GrowbotScreen />);
    expect(await screen.findByText(/growbot is down — no robot can connect/)).toBeInTheDocument();
    expect(screen.getByText("can't connect — growbot is down")).toBeInTheDocument();
  });

  it("says how to start when there is no robot yet", async () => {
    serve({ service: "up", connected: 0, robots: [] });
    render(<GrowbotScreen />);
    expect(await screen.findByText("No robots yet")).toBeInTheDocument();
  });

  it("revokes only on a second tap within 3 s, and disarms on its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const revoked = robot({ revoked_at: new Date(NOW).toISOString(), connected: false });
    const calls = serve({ service: "up", connected: 1, robots: [robot()] }, (path) =>
      path === "/api/growbot/robots/gb-3fa91c/revoke" ? json(revoked) : null,
    );
    render(<GrowbotScreen />);
    const first = await screen.findByRole("button", { name: "Revoke key" });
    fireEvent.click(first);
    expect(screen.getByRole("button", { name: "Tap again to revoke" })).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(3100);
    });
    expect(screen.getByRole("button", { name: "Revoke key" })).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/revoke"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));
    fireEvent.click(screen.getByRole("button", { name: "Tap again to revoke" }));
    expect(await screen.findByRole("button", { name: "Reflash Quorra" })).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([
      "/api/growbot/robots/gb-3fa91c/revoke",
    ]);
    expect(screen.getByText(/Quorra's key is revoked/)).toBeInTheDocument();
  });

  it("explains the certificate the first time, then opens the tab inside the tap", async () => {
    const tab = { closed: false, opener: {} as unknown, location: { href: "" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    const calls = serve({ service: "up", connected: 1, robots: [robot()] }, (path) =>
      path === "/api/growbot/ticket"
        ? json({ ticket: "t", url: "https://jbrain.local:8444/open?ticket=t&id=gb-3fa91c" })
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open GrowBot" }));

    const note = screen.getByRole("dialog", { name: "First time on this phone" });
    const download = within(note).getByRole("link", { name: "Download the box's certificate" });
    expect(download).toHaveAttribute("href", "/api/lan/root.crt");
    expect(open).not.toHaveBeenCalled();

    fireEvent.click(within(note).getByRole("button", { name: "Open GrowBot" }));
    // Opened synchronously in the tap, before the ticket request, or the phone blocks it.
    expect(open).toHaveBeenCalledWith("", "_blank");
    await waitFor(() =>
      expect(tab.location.href).toBe("https://jbrain.local:8444/open?ticket=t&id=gb-3fa91c"),
    );
    // The vendored page on :8444 gets no handle back to the PWA.
    expect(tab.opener).toBeNull();
    expect(calls.find((c) => c.path === "/api/growbot/ticket")?.body).toEqual({
      code: "gb-3fa91c",
    });
    expect(localStorage.getItem(CERT_NOTED_KEY)).toBe("1");

    // Once per phone: the second open goes straight out.
    fireEvent.click(screen.getByRole("button", { name: "Open GrowBot" }));
    expect(screen.queryByRole("dialog", { name: "First time on this phone" })).toBeNull();
    expect(open).toHaveBeenCalledTimes(2);
  });

  it("closes the blank tab when the link can't be made, and says why", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    const tab = { closed: false, location: { href: "" }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    serve({ service: "up", connected: 1, robots: [robot()] }, (path) =>
      path === "/api/growbot/ticket"
        ? json({ detail: "GrowBot needs the box's LAN site (JBRAIN_LAN_ADDR)" }, 409)
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open GrowBot" }));
    expect(await screen.findByText(/GrowBot didn't open — GrowBot needs/)).toBeInTheDocument();
    expect(tab.close).toHaveBeenCalled();
  });

  it("gives a link to tap when the phone blocks the new tab", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    vi.spyOn(window, "open").mockReturnValue(null);
    serve({ service: "up", connected: 1, robots: [robot()] }, (path) =>
      path === "/api/growbot/ticket"
        ? json({ ticket: "t", url: "https://jbrain.local:8444/x" })
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open GrowBot" }));
    const link = await screen.findByRole("link", { name: /tap here to open GrowBot/ });
    expect(link).toHaveAttribute("href", "https://jbrain.local:8444/x");
  });

  it("writes a new key on a second tap and watches it in a sheet", async () => {
    let current: GrowbotFlashJob | null = null;
    const calls = serve({ service: "up", connected: 1, robots: [robot()] }, (path, init) => {
      if (path === "/api/growbot/flash" && init?.method === "POST") {
        current = job();
        return json({ job: current }, 202);
      }
      if (path === "/api/growbot/flash") return json({ job: current });
      return null;
    });
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "New key" }));
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Tap again for a new key" }));

    const sheet = await screen.findByRole("dialog", { name: "New key — Quorra" });
    await waitFor(() =>
      expect(calls.find((c) => c.method === "POST")?.body).toEqual({
        mode: "rekey",
        target: "gb-3fa91c",
      }),
    );
    // A new key goes over MicroPython already on it: no Load row.
    expect(await within(sheet).findByText("Copy its firmware and Wi-Fi")).toBeInTheDocument();
    expect(within(sheet).queryByText("Load MicroPython")).toBeNull();
    expect(within(sheet).getByRole("button", { name: "Writing the new key…" })).toBeDisabled();
    // Closing mid-run leaves the way back in the foot.
    fireEvent.click(within(sheet).getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Flashing — tap to watch" }));
    expect(screen.getByRole("dialog", { name: "New key — Quorra" })).toBeInTheDocument();
  });

  it("words a new key the box refused, rather than a stuck sheet", async () => {
    serve({ service: "up", connected: 1, robots: [robot()] }, (path, init) =>
      path === "/api/growbot/flash" && init?.method === "POST"
        ? json(
            { detail: "Nothing Pico is on the box's USB — plug the robot in, then try again." },
            409,
          )
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "New key" }));
    fireEvent.click(screen.getByRole("button", { name: "Tap again for a new key" }));
    expect(await screen.findByText(/Nothing Pico is on the box's USB/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeEnabled();
  });

  it("opens Add a robot straight away when jpanel handed a Pico over", async () => {
    serve({ service: "up", connected: 0, robots: [] }, (path) =>
      path === "/api/growbot/flash/setup" ? json({ detail: "nope" }, 503) : null,
    );
    const consumed = vi.fn();
    render(<GrowbotScreen openAdd onOpenAddConsumed={consumed} />);
    expect(await screen.findByRole("dialog", { name: "Plug it into the box" })).toBeInTheDocument();
    expect(consumed).toHaveBeenCalled();
  });

  it("reopens a running flash from the foot on its flashing step", async () => {
    serve(
      { service: "up", connected: 0, robots: [] },
      (path) => (path === "/api/growbot/flash/setup" ? json({ detail: "x" }, 503) : null),
      job({
        mode: "flash",
        name: "Pip",
        code: null,
        phases: { load: "run", copy: "wait", join: "wait" },
      }),
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Flashing — tap to watch" }));
    const sheet = screen.getByRole("dialog", { name: "Flashing" });
    expect(within(sheet).getByText("Step 3 of 4")).toBeInTheDocument();
    expect(within(sheet).getByText("Load MicroPython")).toBeInTheDocument();
  });
});

describe("GrowbotScreen without a robot", () => {
  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("opens the creature with no robot, asking the box for a ticket without a code", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    const tab = { closed: false, opener: {} as unknown, location: { href: "" }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    const calls = serve({ service: "up", connected: 0, robots: [] }, (path) =>
      path === "/api/growbot/ticket"
        ? json({ ticket: "t", url: "https://jbrain.local:8444/open?ticket=t" })
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open GrowBot without a robot" }));
    await waitFor(() => expect(tab.location.href).toContain("https://jbrain.local:8444/open"));
    expect(calls.find((c) => c.path === "/api/growbot/ticket")?.body).toEqual({});
    expect(tab.opener).toBeNull();
  });
});

describe("GrowbotScreen signing every phone out", () => {
  afterEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("is armed: the first tap asks again, the second signs every phone out", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    const calls = serve({ service: "up", connected: 0, robots: [] }, (path) =>
      path === "/api/growbot/sessions/reset" ? new Response(null, { status: 204 }) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out every phone" }));
    expect(calls.some((c) => c.path === "/api/growbot/sessions/reset")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Tap again to sign every phone out" }));
    expect(await screen.findByText(/Every phone is signed out of GrowBot/)).toBeTruthy();
    expect(calls.filter((c) => c.path === "/api/growbot/sessions/reset")).toHaveLength(1);
  });
});
