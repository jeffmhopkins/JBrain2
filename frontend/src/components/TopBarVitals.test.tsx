import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TopBarVitals } from "./TopBarVitals";

// The live sources are module state with their own network/timer lifecycles; the chart's
// job is what it DRAWS from them, so they're driven directly here. The strip reads the
// shared ring (latestVitals), which the harness backs with a plain array — pushing a
// sample stands in for one 1 Hz frame landing, and pushing many at once stands in for a
// reconnect backfill. `perSecond` is the real bucketer, deliberately not stubbed.
const gpu = vi.hoisted(() => ({
  value: { percent: null as number | null, state: "unknown" as "reading" | "absent" | "unknown" },
}));
const rate = vi.hoisted(() => ({ value: null as number | null }));
const ring = vi.hoisted(() => ({
  samples: [] as { at: number; gpu: number | null; tps: number | null }[],
}));

vi.mock("../hostVitals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../hostVitals")>()),
  useGpuBusy: () => gpu.value,
  latestVitals: (seconds: number) => {
    const newest = ring.samples[ring.samples.length - 1];
    if (newest === undefined) return [];
    const start = (Math.floor(newest.at / 1000) - seconds + 1) * 1000;
    return ring.samples.filter((s) => s.at >= start);
  },
}));
vi.mock("../agent/tokenMeter", () => ({ useTokenRate: () => rate.value }));

/** Advance the chart's own 1 Hz re-read clock by `seconds`. */
function ticks(seconds: number): void {
  act(() => {
    vi.advanceTimersByTime(seconds * 1000);
  });
}

/** One second of stream: a frame lands in the ring, then the strip's tick re-reads it. */
function frame(gpuValue: number | null, tps: number | null = null): void {
  ring.samples.push({ at: Date.now(), gpu: gpuValue, tps });
  ticks(1);
}

describe("TopBarVitals", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    gpu.value = { percent: null, state: "unknown" };
    rate.value = null;
    ring.samples = [];
  });
  afterEach(() => vi.useRealTimers());

  it("reads out the GPU figure and the sync word", () => {
    gpu.value = { percent: 73, state: "reading" };
    render(<TopBarVitals syncStatus="synced" />);

    expect(screen.getByRole("status")).toHaveTextContent("73");
    expect(screen.getByRole("status")).toHaveTextContent("synced");
  });

  it("keeps the token row's space but hides it until a turn streams", () => {
    gpu.value = { percent: 40, state: "reading" };
    const { container, rerender } = render(<TopBarVitals syncStatus="synced" />);
    expect(container.querySelector(".vitals")).toHaveAttribute("data-turn", "idle");

    rate.value = 48;
    rerender(<TopBarVitals syncStatus="synced" />);

    expect(container.querySelector(".vitals")).toHaveAttribute("data-turn", "live");
    expect(container.querySelector(".vitals-tps")).toHaveTextContent("48");
  });

  it("grows one GPU column per second, up to the window", () => {
    gpu.value = { percent: 50, state: "reading" };
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(0);

    for (let i = 0; i < 3; i += 1) frame(50);
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(3);

    // The axis holds 12 seconds and no more — the oldest column falls off the left.
    for (let i = 0; i < 20; i += 1) frame(50);
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(12);
  });

  it("draws the ring's past at once instead of resampling it column by column", () => {
    // The bug this pins: the strip kept a PRIVATE history, so the seconds a reconnect
    // backfilled into the shared ring (hostVitals) stayed holes in the top bar while
    // the vitals screen's graph filled — the reported PWA resume symptom.
    const now = Date.now();
    for (let i = 11; i >= 0; i -= 1) {
      ring.samples.push({ at: now - i * 1000, gpu: 70, tps: null });
    }
    const { container } = render(<TopBarVitals syncStatus="synced" />);

    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(12);
  });

  it("marks a pinned GPU column hot", () => {
    gpu.value = { percent: 40, state: "reading" };
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    frame(40);
    expect(container.querySelector(".vitals-bar.hot")).toBeNull();

    frame(93);

    expect(container.querySelector(".vitals-bar.hot")).not.toBeNull();
  });

  it("shows a dashed no-gauge line, not an idle GPU, when the box has no amdgpu", () => {
    gpu.value = { percent: null, state: "absent" };
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    ticks(3);

    expect(container.querySelector(".vitals-nogauge")).not.toBeNull();
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(0);
    expect(container.querySelector(".vitals-gpu")).toHaveTextContent("no gpu");
    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("GPU gauge unavailable"),
    );
  });

  it("does not accuse the box of having no GPU when the stream is merely down", () => {
    // The bug this pins: `absent` and `unknown` were one null, so a dropped stream
    // printed "no gpu" on one screen while another screen showed 94%.
    gpu.value = { percent: null, state: "unknown" };
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    ticks(3);

    expect(container.querySelector(".vitals-gpu")).not.toHaveTextContent("no gpu");
    expect(container.querySelector(".vitals-nogauge")).toBeNull();
    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("GPU reading unavailable"),
    );
  });

  it("breaks the token trace over a gap instead of drawing through zero", () => {
    gpu.value = { percent: 20, state: "reading" };
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    frame(20, 50);
    frame(20, 50);
    frame(20, null); // a tool call: nothing generated
    frame(20, null);
    frame(20, 50);
    frame(20, 50);

    // Two separate move commands = two segments, so the pause reads as a gap.
    const path = container.querySelector(".vitals-trace")?.getAttribute("d") ?? "";
    expect(path.match(/M/g)).toHaveLength(2);
  });

  it("freezes instead of draining when frames stop arriving", () => {
    gpu.value = { percent: 60, state: "reading" };
    const { container, rerender } = render(<TopBarVitals syncStatus="synced" />);
    for (let i = 0; i < 4; i += 1) frame(60);
    const drawn = container.querySelectorAll(".vitals-bar").length;

    rerender(<TopBarVitals syncStatus="unreachable" />);
    ticks(6); // seconds pass with nothing arriving

    // Advancing would draw a run of blanks that reads as "the box went idle" when it
    // means "we stopped being told" — so the trace holds where it was. This is what a
    // PWA resume looks like too: the strip holds the pre-suspend picture until the
    // reconnected stream (and its backfill) land, rather than opening on holes.
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(drawn);
    expect(container.querySelector(".vitals")).toHaveAttribute("data-sync", "unreachable");
    expect(container.querySelector(".vitals-sync")).toHaveTextContent("offline");
  });

  it("names the trouble when sync degrades", () => {
    // The rail's colour and the word are driven off data-sync together, so a coloured
    // rule can never appear without the word that explains it.
    for (const [status, word] of [
      ["pending", "pending"],
      ["unreachable", "offline"],
    ] as const) {
      const { container, unmount } = render(<TopBarVitals syncStatus={status} />);
      expect(container.querySelector(".vitals")).toHaveAttribute("data-sync", status);
      expect(container.querySelector(".vitals-sync")).toHaveTextContent(word);
      unmount();
    }
  });

  it("says nothing about a healthy connection", () => {
    const { container } = render(<TopBarVitals syncStatus="synced" />);

    // The word stays in the DOM (the stylesheet drops it from flow off this
    // attribute) so the reading is still there for anything reading the markup.
    expect(container.querySelector(".vitals")).toHaveAttribute("data-sync", "synced");
    expect(container.querySelector(".vitals-sync")).not.toBeNull();
  });

  it("keeps the markup the grid places its rows from", () => {
    // The chart and the GPU figure must land on one grid row — centring them as two
    // separate columns instead put the chart's middle 11px above the figure's. That
    // placement is CSS (so the real check is a browser measurement, not jsdom), but
    // it addresses these exact nodes through the two `display: contents` wrappers.
    // Moving or renaming any of them silently breaks the alignment; this catches it.
    const { container } = render(<TopBarVitals syncStatus="synced" />);

    expect(container.querySelector(".vitals > .vitals-scope > .vitals-chart")).not.toBeNull();
    expect(container.querySelector(".vitals > .vitals-scope > .vitals-sync")).not.toBeNull();
    expect(container.querySelector(".vitals > .vitals-reads > .vitals-tps")).not.toBeNull();
    expect(container.querySelector(".vitals > .vitals-reads > .vitals-gpu")).not.toBeNull();
  });

  it("still reports a healthy connection to a screen reader", () => {
    // Hiding the word is a visual-noise decision; the state itself stays honest.
    render(<TopBarVitals syncStatus="synced" />);

    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("synced"),
    );
  });

  it("describes the whole reading for a screen reader", () => {
    gpu.value = { percent: 71, state: "reading" };
    rate.value = 44;
    render(<TopBarVitals syncStatus="pending" />);

    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      "GPU 71% busy · 44 tokens/sec · sync pending",
    );
  });

  it("stops its clock when unmounted", () => {
    gpu.value = { percent: 50, state: "reading" };
    const { unmount } = render(<TopBarVitals syncStatus="synced" />);
    ticks(2);
    unmount();

    // A torn-down top bar must not keep re-reading behind the screen that replaced it.
    expect(() => ticks(5)).not.toThrow();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("scales the token trace to the window's own peak, not a fixed ceiling", () => {
    // It was pinned at 140 tok/s, so a turn cruising at 20 — a normal rate for a big local
    // model — drew a 14%-high squiggle flat against the baseline, indistinguishable from
    // nothing happening. The number beside it carries magnitude; the trace carries SHAPE.
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    frame(30, 20);
    frame(30, 20);
    frame(30, 10);
    frame(30, 10);
    frame(30, 20);
    frame(30, 20);

    const trace = container.querySelector(".vitals-trace");
    const ys = [...(trace?.getAttribute("d") ?? "").matchAll(/[ML][\d.]+ ([\d.]+)/g)].map((m) =>
      Number(m[1]),
    );
    expect(ys.length).toBeGreaterThanOrEqual(3);
    const top = Math.min(...ys);
    const bottom = Math.max(...ys);
    // A 20/s peak must reach the top of the box, and the 10/s dip must sit near the middle
    // rather than all three flattening into the bottom sliver.
    expect(top).toBeLessThan(2.5);
    expect(bottom - top).toBeGreaterThan(5);
  });
});
