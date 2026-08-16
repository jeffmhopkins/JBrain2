import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./api/client", async (o) => ({
  ...(await o<typeof import("./api/client")>()),
  api: { opsVitals: vi.fn(), opsVitalsStream: vi.fn() },
}));

async function load(): Promise<typeof import("./hostVitals")> {
  vi.resetModules();
  return await import("./hostVitals");
}

describe("seedVitalsHistory", () => {
  afterEach(() => vi.useRealTimers());

  it("fills a hole in the middle of this session's history", async () => {
    // Backgrounding closes the stream but keeps what was already held, so the gap the
    // server DID record sits in the middle — an "older than the earliest" rule drops
    // exactly the samples the ring was added to supply.
    const { seedVitalsHistory, vitalsHistory } = await load();
    const now = Date.now();
    seedVitalsHistory([
      { at_ms: now - 300_000, gpu: 10 },
      { at_ms: now - 150_000, gpu: 55 },
      { at_ms: now - 5_000, gpu: 90 },
    ]);

    const kept = vitalsHistory(600).map((s) => s.gpu);

    expect(kept).toEqual([10, 55, 90]);
  });

  it("ignores seeds stamped in the future by a fast box clock", async () => {
    // Server-stamped, browser-bucketed: a clock running ahead would put an old peak in
    // the newest column and draw it as the current reading.
    const { seedVitalsHistory, vitalsHistory } = await load();
    const now = Date.now();
    seedVitalsHistory([
      { at_ms: now - 1000, gpu: 40 },
      { at_ms: now + 60_000, gpu: 99 },
    ]);

    expect(vitalsHistory(600).map((s) => s.gpu)).toEqual([40]);
  });

  it("does not duplicate a second this session already holds", async () => {
    const { seedVitalsHistory, vitalsHistory } = await load();
    const now = Date.now();
    seedVitalsHistory([{ at_ms: now - 2000, gpu: 30 }]);
    seedVitalsHistory([{ at_ms: now - 2000, gpu: 88 }]);

    const kept = vitalsHistory(600);
    expect(kept).toHaveLength(1);
    expect(kept[0]?.gpu).toBe(30);
  });
});

describe("a stream that dies fatally", () => {
  afterEach(() => vi.useRealTimers());

  /** A stand-in EventSource whose readyState the test drives. */
  class FakeSource {
    static readonly opened: FakeSource[] = [];
    onmessage: ((e: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;
    readyState = 0;
    closed = false;
    close(): void {
      this.closed = true;
    }
    constructor() {
      FakeSource.opened.push(this);
    }
  }

  it("reopens itself after a fatal close instead of staying dead all session", async () => {
    // EventSource retries a DROPPED connection on its own, but a fatal one — the box
    // answering 502 mid-deploy — leaves it CLOSED and it never retries. `source` stayed
    // set through that, so start() short-circuited forever: a deploy blanked the top bar
    // until the app was reloaded, while the detail screen kept showing numbers because it
    // reads the server's ring over plain fetches.
    vi.useFakeTimers();
    FakeSource.opened.length = 0;
    const { api } = await import("./api/client");
    vi.mocked(api.opsVitals).mockResolvedValue({ gpu_busy_percent: 12 });
    vi.mocked(api.opsVitalsStream).mockImplementation(() => new FakeSource() as never);

    const { subscribeGpuBusy } = await load();
    subscribeGpuBusy(() => {});
    await vi.advanceTimersByTimeAsync(0); // let the access probe resolve
    expect(FakeSource.opened).toHaveLength(1);

    const first = FakeSource.opened[0];
    if (first === undefined) throw new Error("no stream opened");
    first.readyState = 2; // CLOSED — will never retry by itself
    first.onerror?.();

    await vi.advanceTimersByTimeAsync(6000);

    expect(first.closed).toBe(true);
    expect(FakeSource.opened).toHaveLength(2);
  });

  it("leaves a merely-dropped stream alone at first, because EventSource retries it", async () => {
    vi.useFakeTimers();
    FakeSource.opened.length = 0;
    const { api } = await import("./api/client");
    vi.mocked(api.opsVitals).mockResolvedValue({ gpu_busy_percent: 12 });
    vi.mocked(api.opsVitalsStream).mockImplementation(() => new FakeSource() as never);

    const { subscribeGpuBusy } = await load();
    subscribeGpuBusy(() => {});
    await vi.advanceTimersByTimeAsync(0);

    const first = FakeSource.opened[0];
    if (first === undefined) throw new Error("no stream opened");
    first.readyState = 0; // CONNECTING — its own retry is in flight
    first.onerror?.();

    await vi.advanceTimersByTimeAsync(1000); // still inside the silence budget

    expect(first.closed).toBe(false);
    expect(FakeSource.opened).toHaveLength(1);
  });

  it("replaces a stream that stays OPEN but stops delivering", async () => {
    // The failure with no error event: a proxy buffering text/event-stream, or a
    // half-open socket after a network change. EventSource reports a healthy connection
    // with nothing behind it, so the fatal-close path never fires — this is what left the
    // top bar blank while the detail screen, on plain fetches, kept showing numbers.
    vi.useFakeTimers();
    FakeSource.opened.length = 0;
    const { api } = await import("./api/client");
    vi.mocked(api.opsVitals).mockResolvedValue({ gpu_busy_percent: 12 });
    vi.mocked(api.opsVitalsStream).mockImplementation(() => new FakeSource() as never);

    const { subscribeGpuBusy } = await load();
    const seen: (number | null)[] = [];
    subscribeGpuBusy((b) => seen.push(b.percent));
    await vi.advanceTimersByTimeAsync(0);

    const first = FakeSource.opened[0];
    if (first === undefined) throw new Error("no stream opened");
    first.readyState = 1; // OPEN, and silent — no error will ever be raised

    await vi.advanceTimersByTimeAsync(12_000);

    expect(first.closed).toBe(true);
    expect(FakeSource.opened.length).toBeGreaterThan(1);
    expect(seen[seen.length - 1]).toBeNull(); // stale is reported as no reading, not 12
  });

  it("keeps a stream that is delivering", async () => {
    vi.useFakeTimers();
    FakeSource.opened.length = 0;
    const { api } = await import("./api/client");
    vi.mocked(api.opsVitals).mockResolvedValue({ gpu_busy_percent: 12 });
    vi.mocked(api.opsVitalsStream).mockImplementation(() => new FakeSource() as never);

    const { subscribeGpuBusy } = await load();
    subscribeGpuBusy(() => {});
    await vi.advanceTimersByTimeAsync(0);

    const first = FakeSource.opened[0];
    if (first === undefined) throw new Error("no stream opened");
    first.readyState = 1;
    for (let i = 0; i < 12; i += 1) {
      first.onmessage?.({ data: '{"gpu_busy_percent": 40}' } as MessageEvent<string>);
      await vi.advanceTimersByTimeAsync(1000);
    }

    expect(first.closed).toBe(false);
    expect(FakeSource.opened).toHaveLength(1);
  });
});
