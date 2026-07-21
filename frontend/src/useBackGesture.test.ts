import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBackGesture } from "./useBackGesture";

// The hook keeps ONE trap entry permanently at the top of history: every platform back
// consumes it, the hook re-arms it and climbs one on-screen layer, and at the root it
// simply stays put — the gesture never exits the app. We drive depth and synthesize the
// platform's popstate to assert the wiring without a device.
describe("useBackGesture", () => {
  let push: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    push = vi.spyOn(window.history, "pushState");
  });
  afterEach(() => vi.restoreAllMocks());

  function pop() {
    act(() => window.dispatchEvent(new PopStateEvent("popstate")));
  }

  it("arms the permanent trap once on mount", () => {
    renderHook(() => useBackGesture(0, vi.fn()));
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("re-arms the trap and closes a layer on each platform back", () => {
    const onBack = vi.fn();
    renderHook(() => useBackGesture(1, onBack));
    push.mockClear();
    pop();
    // The consumed trap is replaced so the next back is caught too, and the top layer closes.
    expect(push).toHaveBeenCalledTimes(1);
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("re-arms but does NOT climb when nothing is open — back stays in the app", () => {
    const onBack = vi.fn();
    renderHook(() => useBackGesture(0, onBack));
    push.mockClear();
    pop();
    // Trap re-armed (so the app can't be backed out of), but there's no layer to close.
    expect(push).toHaveBeenCalledTimes(1);
    expect(onBack).not.toHaveBeenCalled();
  });

  it("closes one layer per back regardless of whether depth strictly decreases", () => {
    // A close that swaps one layer for another (Tasks' return-to-card) keeps depth
    // constant; the permanent trap means the next back still fires onBack.
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 1 },
    });
    pop();
    expect(onBack).toHaveBeenCalledTimes(1);
    rerender({ d: 1 }); // depth unchanged after the swap
    pop();
    expect(onBack).toHaveBeenCalledTimes(2);
  });

  it("reads the latest depth and callback without re-subscribing", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ cb, d }) => useBackGesture(d, cb), {
      initialProps: { cb: first, d: 1 },
    });
    rerender({ cb: second, d: 1 });
    pop();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
