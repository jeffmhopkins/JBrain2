import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api/client";
import { useGpuBusy } from "./hostVitals";

const opsVitals = vi.hoisted(() => vi.fn());
const opsVitalsStream = vi.hoisted(() => vi.fn());

vi.mock("./api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/client")>()),
  api: { opsVitals, opsVitalsStream },
}));

/** A stand-in EventSource that a test can push frames through. */
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
  send(gpu: number | null): void {
    act(() => {
      this.onmessage?.({ data: JSON.stringify({ gpu_busy_percent: gpu }) } as MessageEvent<string>);
    });
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

describe("useGpuBusy", () => {
  beforeEach(() => {
    FakeSource.open = [];
    opsVitals.mockReset();
    opsVitalsStream.mockReset().mockImplementation(() => new FakeSource());
    visibility("visible");
  });
  afterEach(() => vi.useRealTimers());

  it("seeds from the probe, then tracks the stream", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 12 });
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(result.current).toBe(12));

    FakeSource.open[0]?.send(88);
    expect(result.current).toBe(88);
  });

  it("never opens a stream for a principal the server rejects", async () => {
    opsVitals.mockRejectedValue(new ApiError(403, "Forbidden"));
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(opsVitals).toHaveBeenCalled());

    // EventSource cannot see a status code and retries on its own forever, so a
    // family member must never get one opened at all.
    expect(opsVitalsStream).not.toHaveBeenCalled();
    expect(result.current).toBeNull();
  });

  it("retries a probe that failed for a reason that might pass", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opsVitals.mockRejectedValueOnce(new ApiError(503, "restarting"));
    opsVitals.mockResolvedValue({ gpu_busy_percent: 7 });

    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(opsVitals).toHaveBeenCalledTimes(1));
    expect(opsVitalsStream).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    await waitFor(() => expect(result.current).toBe(7));
  });

  it("drops the reading rather than holding a number that stopped being true", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current).toBe(40));

    FakeSource.open[0]?.fail();

    expect(result.current).toBeNull();
  });

  it("survives a malformed frame", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current).toBe(40));

    const source = FakeSource.open[0];
    act(() => {
      source?.onmessage?.({ data: "not json" } as MessageEvent<string>);
    });

    expect(result.current).toBe(40);
    expect(source?.closed).toBe(false);
  });

  it("closes the stream when the app goes to the background", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 55 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current).toBe(55));

    visibility("hidden");

    // A phone in a pocket holds no socket open to read a gauge nobody is looking at.
    expect(FakeSource.open[0]?.closed).toBe(true);
    expect(result.current).toBeNull();
  });

  it("reopens without re-probing when the app comes back", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 55 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalledTimes(1));

    visibility("hidden");
    visibility("visible");

    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalledTimes(2));
    expect(opsVitals).toHaveBeenCalledTimes(1); // access already known
  });

  it("reports a reading-shaped response with no reading as absent", async () => {
    // `undefined` passes a `!== null` test, so letting it through rendered a literal
    // NaN in the top bar. The hook promises `number | null` — it must hold that.
    opsVitals.mockResolvedValue({} as { gpu_busy_percent: number | null });
    const { result } = renderHook(() => useGpuBusy());

    await waitFor(() => expect(opsVitalsStream).toHaveBeenCalled());

    expect(result.current).toBeNull();
  });

  it("rejects a non-finite frame rather than charting it", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 30 });
    const { result } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(result.current).toBe(30));

    act(() => {
      FakeSource.open[0]?.onmessage?.({
        data: '{"gpu_busy_percent": "very busy"}',
      } as MessageEvent<string>);
    });

    expect(result.current).toBeNull();
  });

  it("closes the stream on unmount", async () => {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 55 });
    const { unmount } = renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    unmount();

    expect(FakeSource.open[0]?.closed).toBe(true);
  });
});
