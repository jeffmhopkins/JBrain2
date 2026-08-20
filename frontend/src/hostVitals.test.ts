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
  send(gpu: number | null, loading: unknown = null): void {
    this.frame(JSON.stringify({ gpu_busy_percent: gpu, loading }));
  }
  fail(): void {
    act(() => this.onerror?.());
  }
}

function visibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", {
    value: state,
    configurable: true,
  });
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

  // --- coming back from the background ---------------------------------------
  //
  // Reported symptom: a PWA left open for a while shows dashes in the top bar, the vitals
  // card shows live numbers, and only restarting the app fixes it. `visibilitychange` was
  // the ONLY resume trigger, and an iOS standalone PWA often delivers just `pageshow` on a
  // background/foreground round trip — so nothing ever asked the dead stream to reconnect.

  it("reconnects on pageshow, with no visibility event at all", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    // The socket dies silently while the app is suspended: no error event, readyState still
    // claims OPEN. This is the state iOS leaves an EventSource in, and the exact state a
    // readyState check waves through.
    vi.setSystemTime(Date.now() + 60_000);
    window.dispatchEvent(new Event("pageshow"));

    await waitFor(() => expect(FakeSource.open).toHaveLength(2));
  });

  it("recycles a socket that claims to be OPEN but has stopped delivering", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));
    FakeSource.open[0]?.send(91);

    // Frames stop, but nothing errors. Recovery must NOT depend on the silence watchdog:
    // that is a setTimeout, and a backgrounded app's timers are frozen with it — asleep at
    // precisely the moment the socket dies.
    vi.setSystemTime(Date.now() + 30_000);
    window.dispatchEvent(new Event("focus"));

    await waitFor(() => expect(FakeSource.open).toHaveLength(2));
  });

  it("leaves a healthy stream alone however many resume events arrive", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));
    FakeSource.open[0]?.send(91);

    // Several of these fire for one real resume, so the check has to be idempotent —
    // otherwise "recover faster" becomes "reconnect in a loop".
    window.dispatchEvent(new Event("pageshow"));
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));

    expect(FakeSource.open).toHaveLength(1);
  });

  it("a newly mounted reader revives a stream that had gone dead", async () => {
    // Opening the vitals card showed live numbers (it polls) while the top bar behind it
    // stayed on dashes: nothing about mounting a second reader questioned the shared stream.
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    const first = renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    vi.setSystemTime(Date.now() + 60_000);
    renderHook(() => useGpuBusy());

    await waitFor(() => expect(FakeSource.open).toHaveLength(2));
    FakeSource.open[1]?.send(77);
    expect(first.result.current.percent).toBe(77);
  });

  it("does not reconnect while the app is still in the background", async () => {
    const { useGpuBusy } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40 });
    renderHook(() => useGpuBusy());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    visibility("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    // A phone in a pocket fires `online` on every network flap. Holding a socket open to
    // read a gauge nobody is looking at is the thing visibility gating exists to prevent.
    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("focus"));

    expect(FakeSource.open).toHaveLength(1);
  });
});

describe("useModelLoad", () => {
  beforeEach(() => {
    FakeSource.open = [];
    opsVitals.mockReset();
    opsVitalsStream.mockReset().mockImplementation(() => new FakeSource());
    visibility("visible");
  });
  afterEach(() => vi.useRealTimers());

  it("rides the gauge's stream rather than opening one of its own", async () => {
    // The whole point of putting the load on this frame. The chat status line and the top
    // bar want different fields of the same 1 Hz reading; giving either its own connection
    // would re-create the duplication that once burned the per-origin socket cap.
    const { useGpuBusy, useModelLoad } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40, loading: null });

    const gauge = renderHook(() => useGpuBusy());
    const line = renderHook(() => useModelLoad());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    FakeSource.open[0]?.send(94, {
      model: "gpt-oss-120b",
      at_ms: 1000,
      percent: 0.43,
      kind: "model_load",
    });

    expect(gauge.result.current.percent).toBe(94);
    expect(line.result.current).toEqual({
      model: "gpt-oss-120b",
      at_ms: 1000,
      percent: 0.43,
      kind: "model_load",
    });
  });

  it("holds the stream open for a reader that only wants the load", async () => {
    // The chat status line may be the only thing watching — the top bar's chart is a
    // separate reader, and a screen can be showing one without the other.
    const { useModelLoad } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: null, loading: null });

    const line = renderHook(() => useModelLoad());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    FakeSource.open[0]?.send(null, {
      model: "qwen35",
      at_ms: 20,
      percent: null,
      kind: "model_load",
    });
    expect(line.result.current).toEqual({
      model: "qwen35",
      at_ms: 20,
      percent: null,
      kind: "model_load",
    });

    line.unmount();
    expect(FakeSource.open[0]?.closed).toBe(true);
  });

  it("opens on the probe's answer, not a second later", async () => {
    // The probe's reading is what is published until the first frame arrives. A chat line
    // that read "Thinking it through" for that second — during a load, the one time it is
    // wrong — would flicker into the truth rather than open on it.
    const { useModelLoad } = await load();
    opsVitals.mockResolvedValue({
      gpu_busy_percent: 94,
      loading: { model: "gpt-oss-120b", at_ms: 5, percent: 0.1 },
    });

    const line = renderHook(() => useModelLoad());

    await waitFor(() =>
      expect(line.result.current).toEqual({
        model: "gpt-oss-120b",
        at_ms: 5,
        percent: 0.1,
        kind: "model_load",
      }),
    );
  });

  it("reads a load with no parseable fraction as a load, not as 0%", async () => {
    // A gateway build that prints no progress line is the ordinary case. It still has a
    // model and a start time worth showing.
    const { useModelLoad } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40, loading: null });

    const line = renderHook(() => useModelLoad());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    FakeSource.open[0]?.send(94, { model: "gpt-oss-120b", at_ms: 1000 });
    expect(line.result.current).toEqual({
      model: "gpt-oss-120b",
      at_ms: 1000,
      percent: null,
      kind: "model_load",
    });
  });

  it("ignores a frame from a box too old to send the field, or one half-populated", async () => {
    const { useModelLoad } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40, loading: null });

    const line = renderHook(() => useModelLoad());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));

    FakeSource.open[0]?.frame(JSON.stringify({ gpu_busy_percent: 94 }));
    expect(line.result.current).toBeNull();

    // A name with no clock behind it would put a line on screen counting from nowhere.
    FakeSource.open[0]?.send(94, { model: "gpt-oss-120b" });
    expect(line.result.current).toBeNull();
  });

  it("drops the load when the stream stops delivering", async () => {
    // The one lie this line cannot afford. A frame from four minutes ago naming a load
    // that has long since finished would leave "Loading gpt-oss-120b…" up forever.
    const { useModelLoad } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40, loading: null });

    const line = renderHook(() => useModelLoad());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));
    FakeSource.open[0]?.send(94, {
      model: "gpt-oss-120b",
      at_ms: 1000,
      percent: 0.43,
      kind: "model_load",
    });
    expect(line.result.current).not.toBeNull();

    FakeSource.open[0]?.fail();

    expect(line.result.current).toBeNull();
  });

  it("drops the load when the app is backgrounded", async () => {
    const { useModelLoad } = await load();
    opsVitals.mockResolvedValue({ gpu_busy_percent: 40, loading: null });

    const line = renderHook(() => useModelLoad());
    await waitFor(() => expect(FakeSource.open).toHaveLength(1));
    FakeSource.open[0]?.send(94, {
      model: "gpt-oss-120b",
      at_ms: 1000,
      percent: 0.43,
      kind: "model_load",
    });

    visibility("hidden");

    expect(line.result.current).toBeNull();
  });
});

describe("where a sample lands on the graph's grid", () => {
  beforeEach(() => {
    FakeSource.open = [];
    opsVitals.mockReset();
    opsVitalsStream.mockReset().mockImplementation(() => new FakeSource());
    visibility("visible");
  });
  afterEach(() => vi.useRealTimers());

  /** Open the shared stream and hand back the fake behind it. */
  async function streaming(): Promise<{
    source: FakeSource;
    module: typeof import("./hostVitals");
  }> {
    opsVitals.mockResolvedValue({ gpu_busy_percent: 12 });
    const module = await load();
    module.subscribeGpuBusy(() => {});
    await vi.advanceTimersByTimeAsync(0); // let the access probe resolve
    const source = FakeSource.open[0];
    if (source === undefined) throw new Error("no stream opened");
    return { source, module };
  }

  it("spaces samples by the box's clock, not by when the frames turned up", async () => {
    // THE dropout bug. The box reads the gauge once a second and the graph gives each
    // whole second one slot, but samples were bucketed by the instant the frame ARRIVED:
    // a few hundred milliseconds of jitter put two readings in one second's slot and left
    // the next empty, and the trace showed a gap where nothing had been missed.
    vi.useFakeTimers();
    const start = 1_800_000_000_000;
    vi.setSystemTime(start);
    const { source, module } = await streaming();
    const boxAt = start - 200; // the box's clock, a little behind this browser's

    source.frame(
      JSON.stringify({
        gpu_busy_percent: 10,
        samples: [{ at_ms: boxAt, gpu: 10 }],
      }),
    );
    vi.setSystemTime(start + 1300); // …and the next frame is late
    source.frame(
      JSON.stringify({
        gpu_busy_percent: 20,
        samples: [{ at_ms: boxAt + 1000, gpu: 20 }],
      }),
    );
    vi.setSystemTime(start + 1700); // …and the one after it early, catching up
    source.frame(
      JSON.stringify({
        gpu_busy_percent: 30,
        samples: [{ at_ms: boxAt + 2000, gpu: 30 }],
      }),
    );

    const kept = module.vitalsHistory(60);
    expect(kept.map((s) => s.gpu)).toEqual([10, 20, 30]);
    const [first, second, third] = kept;
    // Exactly a second apart however unevenly the frames landed — which is what puts one
    // sample in each of the plot's per-second slots.
    expect((second?.at ?? 0) - (first?.at ?? 0)).toBe(1000);
    expect((third?.at ?? 0) - (second?.at ?? 0)).toBe(1000);
  });

  it("places a sample from a box whose clock is minutes ahead", async () => {
    // Stamped on the box and bucketed here: an uncorrected offset would push every
    // sample past "now", where the plot drops it, and the trace would never fill.
    vi.useFakeTimers();
    const start = 1_800_000_000_000;
    vi.setSystemTime(start);
    const { source, module } = await streaming();

    source.frame(
      JSON.stringify({
        gpu_busy_percent: 55,
        samples: [{ at_ms: start + 120_000, gpu: 55 }],
      }),
    );

    const kept = module.vitalsHistory(60);
    expect(kept).toHaveLength(1);
    expect(kept[0]?.at).toBeLessThanOrEqual(start);
  });

  it("still records a frame from a box that stamps nothing", async () => {
    // An older api sends the reading alone. A jittery trace beats no trace.
    vi.useFakeTimers();
    const start = 1_800_000_000_000;
    vi.setSystemTime(start);
    const { source, module } = await streaming();

    source.send(77);

    expect(module.vitalsHistory(60)).toEqual([{ at: start, gpu: 77, tps: null }]);
  });

  it("does not record the same second twice when a stream reconnects", async () => {
    // A fresh connection re-offers its newest sample, which this session may already
    // hold. Kept twice, the duplicate evicts a real second from the far end of the ring.
    vi.useFakeTimers();
    const start = 1_800_000_000_000;
    vi.setSystemTime(start);
    const { source, module } = await streaming();
    const frame = JSON.stringify({
      gpu_busy_percent: 41,
      samples: [{ at_ms: start - 500, gpu: 41 }],
    });

    source.frame(frame);
    source.frame(frame);

    expect(module.vitalsHistory(60)).toHaveLength(1);
  });

  it("keeps the higher reading when one second is offered twice", async () => {
    // A load gauge: under-reporting a spike is the worse lie, and the plot's own
    // bucketing resolves a repeated second the same way.
    vi.useFakeTimers();
    const start = 1_800_000_000_000;
    vi.setSystemTime(start);
    const { source, module } = await streaming();

    source.frame(
      JSON.stringify({
        gpu_busy_percent: 4,
        samples: [{ at_ms: start - 500, gpu: 4 }],
      }),
    );
    source.frame(
      JSON.stringify({
        gpu_busy_percent: 96,
        samples: [{ at_ms: start - 400, gpu: 96 }],
      }),
    );

    expect(module.vitalsHistory(60).map((s) => s.gpu)).toEqual([96]);
  });

  it("keeps every sample of a catch-up frame after a late tick", async () => {
    // The stream hands over everything since the last frame it sent, so a tick that ran
    // late costs latency rather than a second of history.
    vi.useFakeTimers();
    const start = 1_800_000_000_000;
    vi.setSystemTime(start);
    const { source, module } = await streaming();

    source.frame(
      JSON.stringify({
        gpu_busy_percent: 30,
        samples: [
          { at_ms: start - 2000, gpu: 10 },
          { at_ms: start - 1000, gpu: 20 },
          { at_ms: start, gpu: 30 },
        ],
      }),
    );

    expect(module.vitalsHistory(60).map((s) => s.gpu)).toEqual([10, 20, 30]);
  });
});
