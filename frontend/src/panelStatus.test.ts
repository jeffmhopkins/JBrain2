/** The judgements in the fleet view, held to what they are FOR.
 *
 * Every number here already existed in the panel's telemetry; what is new is deciding which
 * of them is worth a line on a phone and how long a silence has to be before it means
 * something. Those are the parts that can be wrong in a way nobody notices — a card that
 * cries wolf gets ignored exactly when it matters, and one that stays quiet through a real
 * fault is worse than no card at all.
 */

import { describe, expect, it } from "vitest";
import {
  PANEL_REPORT_S,
  agoLabel,
  panelConcerns,
  panelFacts,
  panelHealth,
  panelStateWords,
} from "./panelStatus";

describe("panelHealth", () => {
  it("does not cry wolf over one missed report", () => {
    /* A radio in a bedroom misses a cycle. If that painted the card red, the owner would
       learn to ignore it before the first real fault arrived. */
    expect(panelHealth(60)).toBe("ok");
    expect(panelHealth(PANEL_REPORT_S + 30)).toBe("ok");
    expect(panelHealth(PANEL_REPORT_S * 2)).toBe("ok");
  });

  it("escalates as the silence gets long enough to mean something", () => {
    expect(panelHealth(PANEL_REPORT_S * 3)).toBe("late");
    expect(panelHealth(PANEL_REPORT_S * 8)).toBe("late");
    expect(panelHealth(PANEL_REPORT_S * 9)).toBe("silent");
    expect(panelHealth(86_400)).toBe("silent");
  });

  it("separates a panel that never reported from one that stopped", () => {
    /* Different faults with different first moves: one was flashed and never came up — check
       it was provisioned against this box at all — the other was working and stopped. */
    expect(panelHealth(-1)).toBe("never");
  });

  it("only ever gets worse as the silence grows", () => {
    const rank = { ok: 0, late: 1, silent: 2, never: -1 } as const;
    let last = rank[panelHealth(0)];
    for (let t = 0; t <= PANEL_REPORT_S * 20; t += 60) {
      const now = rank[panelHealth(t)];
      expect(now).toBeGreaterThanOrEqual(last);
      last = now;
    }
  });
});

describe("agoLabel", () => {
  it("is coarse on purpose", () => {
    /* Nobody needs the seconds, and a precise number invites reading a meaning into the
       difference between 61 and 64. */
    expect(agoLabel(5)).toBe("just now");
    expect(agoLabel(240)).toBe("4 min ago");
    expect(agoLabel(3600)).toBe("1 hour ago");
    expect(agoLabel(9 * 3600)).toBe("9 hours ago");
    expect(agoLabel(3 * 86_400)).toBe("3 days ago");
  });

  it("says never rather than a number for a panel that has not reported", () => {
    expect(agoLabel(-1)).toBe("never");
  });
});

describe("panelFacts", () => {
  it("leads with the screen stage, because it is what prevents a wrong diagnosis", () => {
    /* A sleeping screen stops blitting on purpose, so `blit_ok` stops climbing — the exact
       signature of the stalled render task that cost 0.2.44 a photograph from the owner. */
    expect(panelFacts({ screen: "dark", uptime_ms: 7_200_000 })[0]).toBe("screen dark");
  });

  it("reads an uptime as a length rather than a number of milliseconds", () => {
    expect(panelFacts({ uptime_ms: 7_200_000 })).toContain("up 2h");
    expect(panelFacts({ uptime_ms: 300_000 })).toContain("up 5 min");
  });

  it("prefers the restart reason over the reset reason", () => {
    /* All three callers of `esp_restart()` arrive as `reset_reason: "sw(3)"`, and two of them
       also share a crash phase — the panel's own word for which it was is the only one that
       separates a four-year-old's gesture from a real fault. */
    const facts = panelFacts({ reset_reason: "sw(3)", restart_why: "gesture" });
    expect(facts).toContain("last restart: gesture");
    expect(facts.some((f) => f.includes("sw(3)"))).toBe(false);
  });

  it("says nothing about fields an older panel did not send", () => {
    expect(panelFacts({})).toEqual([]);
  });
});

describe("panelConcerns", () => {
  it("is empty for a healthy panel, which is the usual answer", () => {
    expect(
      panelConcerns({
        screen: "dark",
        blit_ok: 41233,
        blit_fail: 0,
        wifi_drops: 0,
        mic_peak: 9123,
        crash_phase: -1,
        panel_reset: true,
      }),
    ).toEqual([]);
  });

  it("puts a failing update first", () => {
    /* A panel that CANNOT install retries every fifteen minutes forever reporting the old
       version, which from the box is indistinguishable from one nobody offered an update to —
       and "did the update land" is the question this card was built for. */
    const out = panelConcerns({
      ota_err: "ESP_ERR_OTA_VALIDATE_FAILED",
      ota_tries: 12,
      blit_fail_total: 3,
    });
    expect(out[0]).toContain("update failing");
    expect(out[0]).toContain("12 tries");
  });

  it("reports the display fault and the stage it restarted from", () => {
    const out = panelConcerns({ blit_fail_total: 249, blit_recov: 1, crash_phase: 9 });
    expect(out.some((c) => c.includes("249 failed frames") && c.includes("1 self-heals"))).toBe(
      true,
    );
    expect(out.some((c) => c.includes("render stage 9"))).toBe(true);
  });

  it("treats a cold boot's crash phase as healthy rather than as stage -1", () => {
    expect(panelConcerns({ crash_phase: -1 })).toEqual([]);
  });

  it("does not paint the card red every quiet night", () => {
    /* A panel alone in a dark bedroom legitimately hears nothing, and the backend's own note
       says a dead capture path is zero "across several reports while someone is talking near
       it" — neither half of which a snapshot can see. It is a fact, not an alarm. */
    expect(panelConcerns({ mic_peak: 0 })).toEqual([]);
    expect(panelFacts({ mic_peak: 0 })).toContain("heard nothing since the last report");
    expect(panelFacts({ mic_peak: 12 }).some((f) => f.includes("heard nothing"))).toBe(false);
  });

  it("does not flag a missing hardware-reset flag as a fault", () => {
    /* `panel_reset` defaults to false on the box, so firmware too old to send it reports
       false — and would be accused of a fault it may not have. */
    expect(panelConcerns({ panel_reset: false })).toEqual([]);
  });

  it("tolerates a few Wi-Fi drops and speaks up about many", () => {
    expect(panelConcerns({ wifi_drops: 1 })).toEqual([]);
    expect(panelConcerns({ wifi_drops: 7, wifi_reason: 201 })[0]).toContain("7 Wi-Fi drops");
  });

  it("says nothing at all about a report from firmware too old to send these fields", () => {
    /* A fleet view that broke on a panel mid-upgrade would be useless during exactly the
       event it exists to watch. */
    expect(panelConcerns({})).toEqual([]);
  });
});

describe("the state in words", () => {
  /* The Panels tab used to say all of this by fading the card to 75% opacity, which is what a
     healthy panel looks like too, only greyer. `docs/reference/DESIGN.md` is binding: colour
     never carries a meaning on its own. These are the words that carry it instead. */

  it("names the two states a timestamp alone does not explain", () => {
    /* "45 min ago" and "10 hours ago" are durations. Whether either is a problem is a judgement
       about a 15-minute report cycle that the reader should not have to make on a phone. */
    expect(panelStateWords("late")).toBe("late");
    expect(panelStateWords("silent")).toBe("not reporting");
  });

  it("says nothing about a healthy panel", () => {
    /* "Reporting" on every row answers a question nobody asked, and it would make the rows that
       DO need reading look like the ones that do not. */
    expect(panelStateWords("ok")).toBe("");
  });

  it("says nothing about a panel that has never reported, because the timestamp already did", () => {
    /* The seen slot holds the WORD "never reported" for this one rather than a duration, so a
       second phrase beside it would be the same fact twice. That the slot really does carry a
       word here is what makes the silence safe — `agoLabel` is the other half of this pair. */
    expect(panelStateWords("never")).toBe("");
    expect(agoLabel(-1)).toBe("never");
  });

  it("covers every health this file can return", () => {
    /* A new health with no words is a row whose only signal is a colour, which is the defect
       this pair of functions exists to prevent — so the gate is exhaustiveness, not a list. */
    for (const s of [0, PANEL_REPORT_S * 3, PANEL_REPORT_S * 20, -1]) {
      expect(typeof panelStateWords(panelHealth(s))).toBe("string");
    }
  });
});
