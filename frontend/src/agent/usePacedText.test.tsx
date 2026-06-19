import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setTokenRate } from "../tokenRate";
import { usePacedText } from "./usePacedText";

// A controllable requestAnimationFrame: callbacks queue here and only run when a
// test pumps a frame with an explicit timestamp, so the drip is deterministic.
let queue: Array<{ id: number; cb: FrameRequestCallback }> = [];
let nextId = 0;

function frame(now: number): void {
  const pending = queue;
  queue = [];
  act(() => {
    for (const f of pending) f.cb(now);
  });
}

beforeEach(() => {
  queue = [];
  nextId = 0;
  localStorage.clear();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    nextId += 1;
    queue.push({ id: nextId, cb });
    return nextId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    queue = queue.filter((f) => f.id !== id);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("usePacedText", () => {
  it("reveals streamed text gradually, catching up to the full target", () => {
    const { result, rerender } = renderHook(({ t, s }) => usePacedText(t, s), {
      initialProps: { t: "", s: true },
    });
    rerender({ t: "hello world", s: true });

    frame(0); // first frame only establishes the time baseline
    expect(result.current).toBe("");

    frame(42); // ~5 chars at the default 30 t/s (≈120 chars/s)
    expect(result.current.length).toBeGreaterThan(0);
    expect(result.current.length).toBeLessThan("hello world".length);
    expect("hello world".startsWith(result.current)).toBe(true);

    frame(2000); // plenty of time — fully caught up
    expect(result.current).toBe("hello world");
  });

  it("snaps to the full text the moment the turn settles", () => {
    const { result, rerender } = renderHook(({ t, s }) => usePacedText(t, s), {
      initialProps: { t: "", s: true },
    });
    rerender({ t: "the whole answer", s: true });
    frame(0);
    expect(result.current).toBe("");

    rerender({ t: "the whole answer", s: false });
    expect(result.current).toBe("the whole answer");
  });

  it("shows everything at once when pacing is set to instant", () => {
    setTokenRate(0);
    const { result, rerender } = renderHook(({ t, s }) => usePacedText(t, s), {
      initialProps: { t: "", s: true },
    });
    rerender({ t: "no pacing here", s: true });
    expect(result.current).toBe("no pacing here");
  });
});
