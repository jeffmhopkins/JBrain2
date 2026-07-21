import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBackGesture } from "./useBackGesture";

// The hook mirrors the open-layer stack into history and keeps one permanent root trap
// beneath it: `depth + 1` of our entries above the base. Backing out of a layer pops a
// real entry (landing on another of ours, never the base); a bare-screen back pops the
// root trap and we re-arm it. We spy on the history calls and synthesize popstate to
// assert the wiring without a device.
describe("useBackGesture", () => {
  let push: ReturnType<typeof vi.spyOn>;
  let go: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    push = vi.spyOn(window.history, "pushState");
    go = vi.spyOn(window.history, "go").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  function pop() {
    act(() => window.dispatchEvent(new PopStateEvent("popstate")));
  }

  it("arms depth+1 entries on mount (one root trap when nothing is open)", () => {
    renderHook(() => useBackGesture(0, vi.fn()));
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("arms one entry per open layer, plus the root trap", () => {
    renderHook(() => useBackGesture(2, vi.fn()));
    expect(push).toHaveBeenCalledTimes(3);
  });

  it("pushes one fresh entry as each new layer opens", () => {
    const { rerender } = renderHook(({ d }) => useBackGesture(d, vi.fn()), {
      initialProps: { d: 0 },
    });
    expect(push).toHaveBeenCalledTimes(1); // root trap
    rerender({ d: 1 });
    expect(push).toHaveBeenCalledTimes(2);
    rerender({ d: 2 });
    expect(push).toHaveBeenCalledTimes(3);
  });

  it("climbs one layer on the platform back while layers remain", () => {
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 1 },
    });
    push.mockClear();
    pop();
    expect(onBack).toHaveBeenCalledTimes(1);
    // The layer's own entry was consumed natively; closing it drops depth, and the sync
    // leaves the root trap in place (no re-push needed).
    rerender({ d: 0 });
    expect(push).not.toHaveBeenCalled();
  });

  it("re-arms the root trap and does NOT climb on a bare-screen back", () => {
    const onBack = vi.fn();
    renderHook(() => useBackGesture(0, onBack));
    push.mockClear();
    pop();
    expect(push).toHaveBeenCalledTimes(1); // root trap re-armed → stays in the app
    expect(onBack).not.toHaveBeenCalled();
  });

  it("tops the entries back up when a close swaps one layer for another (constant depth)", () => {
    // Tasks' return-to-card: the back closes the session but reveals the Tasks card, so
    // depth stays 1. The consumed entry must be replaced or the next back would hit base.
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 1 },
    });
    pop();
    expect(onBack).toHaveBeenCalledTimes(1);
    push.mockClear();
    rerender({ d: 1 }); // depth unchanged after the swap
    // The sync runs every render, so it re-pushes the entry the back consumed.
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("unwinds surplus entries on a UI-driven close without climbing a layer", () => {
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 2 },
    });
    go.mockClear();
    rerender({ d: 1 }); // a swipe/chevron closed one layer
    expect(go).toHaveBeenCalledWith(-1);
    // The popstate our go() raises is muted — it must not close a second layer.
    pop();
    expect(onBack).not.toHaveBeenCalled();
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
