import { act, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Launcher, type LauncherHandle } from "./Launcher";

// The back gesture closes the launcher through this imperative handle so it
// plays the same slide-down retreat as swipe-down/Escape, not an abrupt unmount.
describe("Launcher imperative close", () => {
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

  it("ref.close() plays the retreat, then settles via onClose", () => {
    const onClose = vi.fn();
    const ref = createRef<LauncherHandle>();
    render(<Launcher ref={ref} open onClose={onClose} onNavigate={() => {}} />);

    const nav = screen.getByRole("navigation", { name: "Launcher" });
    expect(nav).not.toHaveClass("launcher-closing");

    act(() => ref.current?.close());
    // Mid-animation: the slide-down is running and the parent isn't told yet.
    expect(nav).toHaveClass("launcher-closing");
    expect(onClose).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(150));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes immediately when reduced motion is preferred", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const onClose = vi.fn();
    const ref = createRef<LauncherHandle>();
    render(<Launcher ref={ref} open onClose={onClose} onNavigate={() => {}} />);

    act(() => ref.current?.close());
    expect(onClose).toHaveBeenCalledTimes(1); // no animation frame to wait on
  });
});
