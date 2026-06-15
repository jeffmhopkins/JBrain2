import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Launcher } from "./Launcher";

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
