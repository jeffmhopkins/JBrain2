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
