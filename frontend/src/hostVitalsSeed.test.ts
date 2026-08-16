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
