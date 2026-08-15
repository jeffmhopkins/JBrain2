import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TopBarVitals } from "./TopBarVitals";

// The two live sources are hooks with their own network/timer lifecycles; the chart's
// job is what it DRAWS from them, so they're driven directly here.
const gpu = vi.hoisted(() => ({ value: null as number | null }));
const rate = vi.hoisted(() => ({ value: null as number | null }));

vi.mock("../hostVitals", () => ({ useGpuBusy: () => gpu.value }));
vi.mock("../agent/tokenMeter", () => ({ useTokenRate: () => rate.value }));

/** Advance the chart's own 1 Hz sampling clock by `seconds`. */
function ticks(seconds: number): void {
  act(() => {
    vi.advanceTimersByTime(seconds * 1000);
  });
}

describe("TopBarVitals", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    gpu.value = null;
    rate.value = null;
  });
  afterEach(() => vi.useRealTimers());

  it("reads out the GPU figure and the sync word", () => {
    gpu.value = 73;
    render(<TopBarVitals syncStatus="synced" />);

    expect(screen.getByRole("status")).toHaveTextContent("73");
    expect(screen.getByRole("status")).toHaveTextContent("synced");
  });

  it("keeps the token row's space but hides it until a turn streams", () => {
    gpu.value = 40;
    const { container, rerender } = render(<TopBarVitals syncStatus="synced" />);
    expect(container.querySelector(".vitals")).toHaveAttribute("data-turn", "idle");

    rate.value = 48;
    rerender(<TopBarVitals syncStatus="synced" />);

    expect(container.querySelector(".vitals")).toHaveAttribute("data-turn", "live");
    expect(container.querySelector(".vitals-tps")).toHaveTextContent("48");
  });

  it("grows one GPU column per second, up to the window", () => {
    gpu.value = 50;
    const { container } = render(<TopBarVitals syncStatus="synced" />);
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(0);

    ticks(3);
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(3);

    // The axis holds 12 seconds and no more — the oldest column falls off the left.
    ticks(20);
    expect(container.querySelectorAll(".vitals-bar")).toHaveLength(12);
  });

  it("marks a pinned GPU column hot", () => {
    gpu.value = 40;
    const { container, rerender } = render(<TopBarVitals syncStatus="synced" />);
    ticks(1);
    expect(container.querySelector(".vitals-bar.hot")).toBeNull();

    // A new frame from the stream re-renders before the next sampling tick, which is
    // what publishes the value the tick then reads.
    gpu.value = 93;
    rerender(<TopBarVitals syncStatus="synced" />);
    ticks(1);

    expect(container.querySelector(".vitals-bar.hot")).not.toBeNull();
  });

  it("shows a dashed no-gauge line, not an idle GPU, when the box has no amdgpu", () => {
    gpu.value = null;
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

  it("breaks the token trace over a gap instead of drawing through zero", () => {
    gpu.value = 20;
    rate.value = 50;
    const { container, rerender } = render(<TopBarVitals syncStatus="synced" />);
    ticks(2);

    rate.value = null; // a tool call: nothing generated
    rerender(<TopBarVitals syncStatus="synced" />);
    ticks(2);

    rate.value = 50;
    rerender(<TopBarVitals syncStatus="synced" />);
    ticks(2);

    // Two separate move commands = two segments, so the pause reads as a gap.
    const path = container.querySelector(".vitals-trace")?.getAttribute("d") ?? "";
    expect(path.match(/M/g)).toHaveLength(2);
  });

  it("freezes the axis while the server is unreachable", () => {
    gpu.value = 60;
    const { container, rerender } = render(<TopBarVitals syncStatus="synced" />);
    ticks(4);
    const drawn = container.querySelectorAll(".vitals-bar").length;

    rerender(<TopBarVitals syncStatus="unreachable" />);
    ticks(6);

    // Advancing would draw a run of blanks that reads as "the box went idle" when it
    // means "we stopped being told" — so the trace holds where it was.
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

    // Quiet, not absent: the word keeps its line box (hidden by the stylesheet off
    // this attribute) so the chart doesn't jump the moment sync degrades.
    expect(container.querySelector(".vitals")).toHaveAttribute("data-sync", "synced");
    expect(container.querySelector(".vitals-sync")).not.toBeNull();
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
    gpu.value = 71;
    rate.value = 44;
    render(<TopBarVitals syncStatus="pending" />);

    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      "GPU 71% busy · 44 tokens/sec · sync pending",
    );
  });

  it("stops its clock when unmounted", () => {
    gpu.value = 50;
    const { unmount } = render(<TopBarVitals syncStatus="synced" />);
    ticks(2);
    unmount();

    // A torn-down top bar must not keep sampling behind the screen that replaced it.
    expect(() => ticks(5)).not.toThrow();
    expect(vi.getTimerCount()).toBe(0);
  });
});
