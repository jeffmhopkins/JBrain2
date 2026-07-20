import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Launcher } from "./Launcher";

// Tiles navigate by their `target`: clicking one routes the parent to the
// matching surface. Workflow is its own first-class System card (promoted out
// of Ops), so it must render and route to the Automations surface.
describe("Launcher tile navigation", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("no network"))),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the Workflow card and routes it to the automations surface", () => {
    const onNavigate = vi.fn();
    render(<Launcher open onClose={() => {}} onNavigate={onNavigate} />);

    fireEvent.click(screen.getByRole("button", { name: "Workflow" }));
    expect(onNavigate).toHaveBeenCalledWith("automations");
  });

  it("still routes the sibling Ops card to ops", () => {
    const onNavigate = vi.fn();
    render(<Launcher open onClose={() => {}} onNavigate={onNavigate} />);

    fireEvent.click(screen.getByRole("button", { name: "Ops" }));
    expect(onNavigate).toHaveBeenCalledWith("ops");
  });

  it("routes the Data card to its launcher screen", () => {
    const onNavigate = vi.fn();
    render(<Launcher open onClose={() => {}} onNavigate={onNavigate} />);

    fireEvent.click(screen.getByRole("button", { name: "Data" }));
    expect(onNavigate).toHaveBeenCalledWith("data");
  });
});

// The Image tile is configuration-gated: it appears only when image hosting is
// enabled (getImageSettings().enabled), mirroring the provider-hidden-when-unkeyed
// pattern. Enablement is fetched once per session and cached, so each case loads a
// fresh module to reset that cache.
describe("Launcher image tile gating", () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  function stubImageSettings(enabled: boolean): void {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        if (path.endsWith("/api/settings/image")) {
          return Promise.resolve(
            new Response(JSON.stringify({ enabled, reachable: enabled, models: [] }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        // Other launcher fetches (review/tasks badges) are unrelated here.
        return Promise.reject(new Error("no network"));
      }),
    );
  }

  it("shows the Image tile when image hosting is enabled", async () => {
    stubImageSettings(true);
    const { Launcher: Fresh } = await import("./Launcher");
    render(<Fresh open onClose={() => {}} onNavigate={() => {}} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Image" })).toBeInTheDocument());
  });

  // Regression: the icon count must not jump on open. When a prior session recorded
  // that image hosting is enabled, the tile is present on the VERY FIRST paint —
  // hydrated synchronously from localStorage, before the async settings fetch runs —
  // so swiping the launcher up never shows a grid that then grows by one tile.
  it("renders the Image tile on the first paint from the cached enablement (no flash)", async () => {
    localStorage.setItem("jb.image.enabled", "true");
    // A never-resolving fetch proves the tile is not waiting on the network.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    const { Launcher: Fresh } = await import("./Launcher");
    render(<Fresh open onClose={() => {}} onNavigate={() => {}} />);
    // No waitFor: it must already be in the document synchronously.
    expect(screen.getByRole("button", { name: "Image" })).toBeInTheDocument();
  });

  it("hides the Image tile when image hosting is disabled", async () => {
    stubImageSettings(false);
    const { Launcher: Fresh } = await import("./Launcher");
    render(<Fresh open onClose={() => {}} onNavigate={() => {}} />);
    // Let the (cached) settings fetch resolve, then confirm the tile stays absent.
    await waitFor(() => expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Image" })).toBeNull();
  });
});

// Any close — X/grab, swipe-down, Escape, or the platform back gesture — clears
// `open` in the parent; the launcher then plays its slide-down retreat and
// unmounts. Closing this controlled way drops the nav depth immediately, so the
// back gesture can't fall through and exit the app mid-animation.
describe("Launcher controlled retreat", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    // The open effect fetches the review badge count; a rejection is swallowed.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("no network"))),
    );
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const noop = () => {};

  it("plays the retreat when open goes false, then unmounts", () => {
    const { rerender } = render(<Launcher open onClose={noop} onNavigate={noop} />);
    expect(screen.getByRole("navigation", { name: "Launcher" })).not.toHaveClass(
      "launcher-closing",
    );

    rerender(<Launcher open={false} onClose={noop} onNavigate={noop} />);
    // Still mounted, mid slide-down.
    expect(screen.getByRole("navigation", { name: "Launcher" })).toHaveClass("launcher-closing");

    act(() => vi.advanceTimersByTime(150));
    expect(screen.queryByRole("navigation", { name: "Launcher" })).toBeNull();
  });

  it("unmounts immediately when reduced motion is preferred", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const { rerender } = render(<Launcher open onClose={noop} onNavigate={noop} />);
    rerender(<Launcher open={false} onClose={noop} onNavigate={noop} />);
    expect(screen.queryByRole("navigation", { name: "Launcher" })).toBeNull();
  });
});

// The Review badge is a live indicator: it fetches on open and keeps polling
// while the launcher stays mounted, so a hold that lands (or clears) shows up
// without reopening the menu.
describe("Launcher review badge (live count)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const noop = () => {};
  const queueOf = (n: number) =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({ items: Array.from({ length: n }, (_, i) => ({ id: `r${i}` })) }),
    } as Response);
  // Flush the pending fetch microtasks (fake timers, so no findBy/waitFor).
  const flush = () => act(async () => void (await vi.advanceTimersByTimeAsync(0)));
  const tick = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));

  it("polls the review count while open and updates the badge", async () => {
    let count = 2;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => queueOf(count)),
    );
    render(<Launcher open onClose={noop} onNavigate={noop} />);

    // Immediate fetch on open paints the current count.
    await flush();
    expect(screen.getByText("2")).toBeInTheDocument();

    // A new hold lands; the next poll tick reflects it — no reopen needed.
    count = 3;
    await tick(10_000);
    expect(screen.getByText("3")).toBeInTheDocument();

    // All cleared: the badge drops away once the count hits zero.
    count = 0;
    await tick(10_000);
    expect(screen.queryByText(/^\d+$/)).toBeNull();
  });

  it("does not poll while a card is stacked over it (active=false)", async () => {
    const fetchMock = vi.fn(() => queueOf(1));
    vi.stubGlobal("fetch", fetchMock);
    // Open but covered by a card: mounted for the reveal beneath, but off-screen.
    render(<Launcher open active={false} onClose={noop} onNavigate={noop} />);
    await flush();
    await tick(30_000);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("resumes polling when the card closes and it's back on screen", async () => {
    const fetchMock = vi.fn(() => queueOf(1));
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<Launcher open active={false} onClose={noop} onNavigate={noop} />);
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();

    rerender(<Launcher open active={true} onClose={noop} onNavigate={noop} />);
    await flush();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(fetchMock.mock.calls.length).toBeGreaterThan(0);
  });

  it("stops polling once closed", async () => {
    const fetchMock = vi.fn(() => queueOf(1));
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<Launcher open onClose={noop} onNavigate={noop} />);
    await flush();
    expect(screen.getByText("1")).toBeInTheDocument();
    const callsWhileOpen = fetchMock.mock.calls.length;

    rerender(<Launcher open={false} onClose={noop} onNavigate={noop} />);
    await tick(150); // play out the retreat + unmount
    await tick(30_000);
    expect(fetchMock.mock.calls.length).toBe(callsWhileOpen);
  });
});

// The Tasks badge counts tasks whose latest run hasn't been opened on THIS device
// (the jb.tasks.viewedRunAt markers that also drive each card's NEW band) — not
// runs since Tasks was last opened. Opening the screen no longer clears it; only
// opening a task's session does.
describe("Launcher tasks badge (unviewed count)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    localStorage.clear();
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  const noop = () => {};
  const flush = () => act(async () => void (await vi.advanceTimersByTimeAsync(0)));

  // Only `id` + `latest_run.started_at` feed the count; keep the rest minimal.
  const task = (id: string, startedAt: string | null) => ({
    id,
    latest_run: startedAt === null ? null : { id: `${id}-run`, started_at: startedAt },
  });

  // The launcher fires two badge fetches: the review queue and the tasks list.
  function stubFetch(tasks: unknown[]): void {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        const body = path.includes("/api/tasks") ? tasks : { items: [] };
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(body),
        } as Response);
      }),
    );
  }

  it("counts tasks with an unviewed latest run, ignoring viewed and never-run ones", async () => {
    const T1 = "2026-07-07T05:00:00.000Z";
    const T2 = "2026-07-06T08:30:00.000Z";
    // "opened" was viewed on this device at its latest run; "never" has no run.
    localStorage.setItem("jb.tasks.viewedRunAt", JSON.stringify({ opened: T1 }));
    stubFetch([
      task("a", T1), // no marker → unviewed
      task("b", T2), // no marker → unviewed
      task("opened", T1), // marker == latest → viewed, not counted
      task("never", null), // never ran → not counted
    ]);
    render(<Launcher open onClose={noop} onNavigate={noop} />);

    await flush();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("shows no badge when every task's latest run has been opened", async () => {
    const T1 = "2026-07-07T05:00:00.000Z";
    localStorage.setItem("jb.tasks.viewedRunAt", JSON.stringify({ a: T1, b: T1 }));
    stubFetch([task("a", T1), task("b", T1)]);
    render(<Launcher open onClose={noop} onNavigate={noop} />);

    await flush();
    expect(screen.queryByText(/^\d+$/)).toBeNull();
  });
});
