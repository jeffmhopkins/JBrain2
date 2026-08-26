import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { isForeground, useForeground, useForegroundRef } from "./visibility";

// jsdom leaves visibilityState read-only; redefine it, then fire the event the
// browser would on a foreground/background flip.
function setVisibility(state: "visible" | "hidden") {
  act(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: state,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

afterEach(() => setVisibility("visible"));

describe("visibility", () => {
  it("reads the document's current foreground state", () => {
    setVisibility("visible");
    expect(isForeground()).toBe(true);
    setVisibility("hidden");
    expect(isForeground()).toBe(false);
  });

  it("useForeground reacts to background/foreground transitions", () => {
    setVisibility("visible");
    const { result } = renderHook(() => useForeground());
    expect(result.current).toBe(true);
    setVisibility("hidden");
    expect(result.current).toBe(false);
    setVisibility("visible");
    expect(result.current).toBe(true);
  });

  it("useForegroundRef tracks the live value without re-rendering", () => {
    setVisibility("visible");
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useForegroundRef();
    });
    const after = renders;
    expect(result.current.current).toBe(true);
    setVisibility("hidden");
    // The ref mutates in place — no extra render is triggered.
    expect(result.current.current).toBe(false);
    expect(renders).toBe(after);
  });

  it("stops listening once unmounted", () => {
    setVisibility("visible");
    const { result, unmount } = renderHook(() => useForeground());
    unmount();
    setVisibility("hidden");
    // The unmounted hook holds its last value; no error from a stale listener.
    expect(result.current).toBe(true);
  });

  it("recovers when a resume delivers only pageshow, with no visibility event", () => {
    // An iOS standalone PWA restored from the page cache after a long absence: the
    // hidden-flip fired on the way out, but no visibilitychange fires on the way back.
    // Every useForeground-gated poll stayed torn down — the vitals screen came back
    // frozen — until the next visibility event, which might be a screen lock away.
    setVisibility("hidden");
    const { result } = renderHook(() => useForeground());
    expect(result.current).toBe(false);

    act(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      window.dispatchEvent(new Event("pageshow"));
    });

    expect(result.current).toBe(true);
  });

  it("does not let a background focus or online event fake a foreground", () => {
    // A phone in a pocket fires `online` on every network flap. The resume events are
    // wake-up calls to RE-READ visibilityState, never evidence of visibility themselves.
    setVisibility("hidden");
    const { result } = renderHook(() => useForeground());

    act(() => {
      window.dispatchEvent(new Event("online"));
      window.dispatchEvent(new Event("focus"));
    });

    expect(result.current).toBe(false);
  });

  it("useForegroundRef hears a pageshow-only resume too", () => {
    setVisibility("hidden");
    const { result } = renderHook(() => useForegroundRef());
    expect(result.current.current).toBe(false);

    act(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      window.dispatchEvent(new Event("pageshow"));
    });

    expect(result.current.current).toBe(true);
  });
});
