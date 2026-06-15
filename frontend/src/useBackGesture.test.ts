import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBackGesture } from "./useBackGesture";

// The hook mirrors the layer stack into history: a trap entry while anything is
// open, consumed by the OS back to close the top layer. We drive depth changes
// and synthesize the platform's popstate to assert the wiring without a device.
describe("useBackGesture", () => {
  let push: ReturnType<typeof vi.spyOn>;
  let back: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    push = vi.spyOn(window.history, "pushState");
    back = vi.spyOn(window.history, "back").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  function pop() {
    act(() => window.dispatchEvent(new PopStateEvent("popstate")));
  }

  it("arms a single history trap when the first layer opens", () => {
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 0 },
    });
    expect(push).not.toHaveBeenCalled();

    rerender({ d: 1 });
    expect(push).toHaveBeenCalledTimes(1);
    // A second layer rides the same trap — no extra entry stacks up.
    rerender({ d: 2 });
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("closes the top layer on the platform back gesture", () => {
    const onBack = vi.fn();
    renderHook(() => useBackGesture(1, onBack));
    pop();
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("re-arms the trap while deeper layers remain, so each back pops one", () => {
    const onBack = vi.fn();
    renderHook(() => useBackGesture(2, onBack));
    push.mockClear();
    pop();
    expect(onBack).toHaveBeenCalledTimes(1);
    // Deeper layer still open → a fresh trap is pushed for the next back.
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("drops the trap (not the layer) when the UI closes the last layer", () => {
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 1 },
    });
    rerender({ d: 0 });
    // History is unwound to match the UI close; onBack is the OS-back path only.
    expect(back).toHaveBeenCalledTimes(1);
    expect(onBack).not.toHaveBeenCalled();
  });

  it("ignores the popstate it raises while unwinding a UI close", () => {
    const onBack = vi.fn();
    const { rerender } = renderHook(({ d }) => useBackGesture(d, onBack), {
      initialProps: { d: 1 },
    });
    rerender({ d: 0 }); // UI close → history.back() → its popstate must be muted
    pop();
    expect(onBack).not.toHaveBeenCalled();
  });

  it("does nothing on a stray popstate when no layers are open", () => {
    const onBack = vi.fn();
    renderHook(() => useBackGesture(0, onBack));
    pop();
    expect(onBack).not.toHaveBeenCalled();
  });
});
