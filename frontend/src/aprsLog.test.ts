// The health reading, which is the load-bearing half of the APRS tab.
//
// A quiet packet channel and a dead receiver are indistinguishable in a list of rows,
// and this surface has already deleted one control for getting that wrong — the tuner's
// meter read `peak` on demodulated audio, so an empty channel full of hiss read HIGH.
// So health is last-decode and rate, never a bar, and these pin what it says.

import { describe, expect, it } from "vitest";
import { type AprsLogState, STALE_AFTER_MS, decodeRate, receiverHealth } from "./aprsLog";

const NOW = Date.UTC(2026, 8, 2, 12, 0, 0);

function state(over: Partial<AprsLogState> = {}): AprsLogState {
  return { logging: true, frequency_hz: 144_390_000, packets: [], ...over };
}

function packet(agoMs: number, info = "GATE 7K2M9") {
  return {
    heard_at: new Date(NOW - agoMs).toISOString(),
    frequency_hz: 144_390_000,
    source: "KE8XYZ-9",
    destination: "APDW17",
    path: ["WIDE1-1"],
    info,
  };
}

describe("is the receiver alive", () => {
  it("says it is off when nothing is logging, even with rows loaded", () => {
    // Rows are history. They say nothing about whether anything is receiving NOW, and
    // reading "alive" off them is exactly the mistake the deleted meter made.
    const health = receiverHealth(state({ logging: false, packets: [packet(1000)] }), NOW);

    expect(health.tone).toBe("off");
  });

  it("separates a quiet channel from a channel that has never been heard", () => {
    expect(receiverHealth(state(), NOW).tone).toBe("quiet");
    expect(receiverHealth(state(), NOW).text).toContain("nothing heard yet");
  });

  it("calls a long silence stale rather than quiet", () => {
    const health = receiverHealth(state({ packets: [packet(STALE_AFTER_MS + 60_000)] }), NOW);

    // A dead receiver must never read as a quiet one: that is the whole point.
    expect(health.tone).toBe("stale");
    expect(health.text).toContain("nothing for");
  });

  it("reads live while packets are recent", () => {
    const health = receiverHealth(state({ packets: [packet(30_000)] }), NOW);

    expect(health.tone).toBe("live");
    expect(health.text).toBe("heard 30s ago");
  });

  it("survives a packet whose timestamp is unparseable", () => {
    const bad = { ...packet(0), heard_at: "not a date" };

    // The row came off the air; a broken field must not throw inside a render.
    expect(() => receiverHealth(state({ packets: [bad] }), NOW)).not.toThrow();
  });
});

describe("decode rate", () => {
  it("is unknown until there is a span to measure", () => {
    expect(decodeRate(state({ packets: [packet(1000)] }), NOW)).toBe("—");
    expect(decodeRate(state({ logging: false }), NOW)).toBe("—");
  });

  it("reports packets per hour over what is loaded", () => {
    const packets = [packet(0), packet(1_800_000)]; // two, across half an hour

    expect(decodeRate(state({ packets }), NOW)).toBe("4 pkt/hr");
  });
});
