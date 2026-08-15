import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api/client";

const opsVitals = vi.hoisted(() => vi.fn());
const opsVitalsStream = vi.hoisted(() => vi.fn());

vi.mock("./api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/client")>()),
  api: { opsVitals, opsVitalsStream },
}));

/** A stand-in EventSource a test can push frames through. */
class FakeSource {
  static open: FakeSource[] = [];
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor() {
    FakeSource.open.push(this);
  }
  close(): void {
    this.closed = true;
  }
  frame(body: string): void {
    act(() => {
      this.onmessage?.({ data: body } as MessageEvent<string>);
    });
  }
  send(gpu: number | null): void {
    this.frame(JSON.stringify({ gpu_busy_percent: gpu }));
  }
  fail(): void {
    act(() => this.onerror?.());
  }
}

function visibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

// The stream is module state shared by the whole app, so each test needs a fresh
// module instance rather than leftover access//subscriber state from the last one.
async function load(): Promise<typeof import("./hostVitals")> {
  vi.resetModules();
  return await import("./hostVitals");
}

describe("useGpuBusy", () => {
  beforeEach(() => {
    FakeSource.open = [];
    opsVitals.mockReset();
    opsVitalsStream.mockReset().mockImplementation(() => new FakeSource());
    visibility("visible");
  });
  afterEach(() => vi.useRealTimers());

  it("opens ONE stream however many readers there are", async () => {
    // The TopBar renders on every screen and App.tsx keeps HomeScreen mounted behind
    // an open card, so several are alive at once. A stream per reader made them
    // disagree — one showed "no gpu" while another showed 94% — and burned through
    // the browser's per-origin connection cap until one of them stayed dead.
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });

    const a = renderHook(() => useGpuBusy());
    const b = renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    expect(opsVitals).toHaveBeenCalledTimes(1);

    FakeSource.open[0]?.send(91);
    expect(a.result.current.percent).toBe(91);
    expect(b.result.current.percent).toBe(91);
  });

  it("keeps the stream while any reader remains, and closes it with the last", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });

    const a = renderHook(() => useGpuBusy());
    const b = renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    a.unmount();
    expect(FakeSource.open[0]?.closed).toBe(false);

    b.unmount();
    expect(FakeSource.open[0]?.closed).toBe(true);
  });

  it("hands a late reader the current figure, not a blank", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));
    FakeSource.open[0]?.send(77);

    const late = renderHook(() => useGpuBusy());

    expect(late.result.current).toEqual({ percent: 77, state: "reading" });
  });

  it("seeds from the probe, then tracks the stream", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 12 });
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(result.current.percent).toBe(12));

    FakeSource.open[0]?.send(88);
    expect(result.current).toEqual({ percent: 88, state: "reading" });
  });

  it("says the reading is unknown when the stream drops — not that the GPU is gone", async () => {
    // Both were `null` once, so a dropped stream rendered the words "no gpu": a claim
    // about the hardware, made when only the connection had failed.
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current.percent).toBe(40));

    FakeSource.open[0]?.fail();

    expect(result.current).toEqual({ percent: null, state: "unknown" });
  });

  it("says the gauge is absent only when the box says so", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: null });
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(result.current.state).toBe("absent"));
    expect(result.current.percent).toBeNull();
  });

  it("treats a reading-shaped response with no reading as absent", async () => {
    // `undefined` passes a `!== null` test, which once rendered a literal NaN.
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({} as { gpu_busy_percent: number | null });
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalled());
    expect(result.current).toEqual({ percent: null, state: "absent" });
  });

  it("rejects a non-finite frame rather than charting it", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 30 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current.percent).toBe(30));

    FakeSource.open[0]?.frame('{"gpu_busy_percent": "very busy"}');

    expect(result.current).toEqual({ percent: null, state: "absent" });
  });

  it("survives a malformed frame", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current.percent).toBe(40));

    FakeSource.open[0]?.frame("not json");

    expect(result.current.percent).toBe(40);
    expect(FakeSource.open[0]?.closed).toBe(false);
  });

  it("degrades instead of taking the top bar down when there is no EventSource", async () => {
    // This runs inside a React effect: a throw here unmounted the entire top bar
    // (and crashed any screen rendering it) over a gauge that is decoration.
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 20 });
    opsVitalsStream.mockImplementation(() => {
      throw new ReferenceError("EventSource is not defined");
    });

    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalled());
    expect(result.current).toEqual({ percent: null, state: "unknown" });
  });

  it("never opens a stream for a principal the server rejects", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockRejectedValue(new ApiError(403, "Forbidden"));
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(opsVitals).toHaveBeenCalled());

    // EventSource cannot see a status code and retries on its own forever, so a
    // family member must never get one opened at all.
    expect(opsVitalsStream).not.toHaveBeenCalled();
    expect(result.current.state).toBe("unknown");
  });

  it("retries a probe that failed for a reason that might pass", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { useGpuBusy } = await load();
    opsVitals.mockRejectedValueOnce(new ApiError(503, "restarting"));
    opsVitals.mockResolvedValue({ gpu_busy_percent: 7 });

    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(opsVitals).toHaveBeenCalledTimes(1));
    expect(opsVitalsStream).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    await waitFor(() => expect(result.current.percent).toBe(7));
  });

  it("closes the stream when the app goes to the background", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 55 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current.percent).toBe(55));

    visibility("hidden");

    // A phone in a pocket holds no socket open to read a gauge nobody is looking at.
    expect(FakeSource.open[0]?.closed).toBe(true);
    expect(result.current.state).toBe("unknown");
  });

  it("reopens without re-probing when the app comes back", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 55 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalledTimes(1));

    visibility("hidden");
    visibility("visible");

    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalledTimes(2));
    expect(opsVitals).toHaveBeenCalledTimes(1); // access already known
  });
});
