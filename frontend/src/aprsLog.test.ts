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
  return {
    logging: true,
    reachable: true,
    frequency_hz: 144_390_000,
    packets: [],
    ...over,
  };
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

  it("survives a packet whose timestamp is unparseable, and does not call it live", () => {
    const bad = { ...packet(0), heard_at: "not a date" };

    // The row came off the air; a broken field must not throw inside a render. It also
    // must not land on the most REASSURING tone — this used to read "heard NaNs ago"
    // with a green dot, which in the one control whose job is telling a dead receiver
    // from a quiet channel is the worst possible answer to "I don't know".
    const health = receiverHealth(state({ packets: [bad] }), NOW);
    expect(health.tone).toBe("stale");
  });

  it("says the radio is unreachable rather than calling it off", () => {
    // "Off" is a state; "we cannot tell" is not. A dead receiver reading as a
    // switched-off one is the confusion this line exists to prevent.
    const health = receiverHealth(state({ reachable: false, logging: false }), NOW);
    expect(health.text).toBe("the radio isn't reachable");
    expect(health.tone).not.toBe("off");
  });
});

describe("decode rate", () => {
  it("is unknown when nothing is receiving", () => {
    expect(decodeRate(state({ logging: false }), NOW)).toBe("—");
    expect(decodeRate(state({ reachable: false }), NOW)).toBe("—");
  });

  it("counts what arrived inside the freshness window", () => {
    const packets = [packet(0), packet(1_800_000)]; // two, both within 40 minutes

    // Two in a 40-minute window is 3 an hour. The window is the SAME one that decides
    // staleness, so the rate and the health line can never disagree.
    expect(decodeRate(state({ packets }), NOW)).toBe("3 pkt/hr");
  });

  it("reads ZERO once the channel goes stale, not busy", () => {
    // The failure this replaced: measuring from the oldest packet to now made the rate
    // decay but never reach zero, so a receiver that had heard nothing for 41 minutes
    // still read "26 pkt/hr" — busy, beside a health line saying "nothing for 41 min".
    // The two halves of the same control contradicting each other.
    const packets = [packet(41 * 60_000), packet(45 * 60_000), packet(50 * 60_000)];

    expect(decodeRate(state({ packets }), NOW)).toBe("0 pkt/hr");
  });
});
