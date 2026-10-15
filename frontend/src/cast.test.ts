import { describe, expect, it } from "vitest";
import type { CastNotice, CastTv } from "./api/client";
import {
  MEDIA_RECEIVER,
  TV_H,
  TV_W,
  canTakeDown,
  draftReady,
  draftTitle,
  dragDraft,
  drawFrame,
  emptyCastDraft,
  outcomeLabel,
  outcomeSub,
  readyCount,
  resultTitle,
  secondsLabel,
  sentSummary,
  skipReason,
  tvWords,
} from "./cast";

function tv(over: Partial<CastTv> = {}): CastTv {
  return {
    id: "t1",
    name: "Big TV",
    model: null,
    state: "idle",
    why: "Ambient",
    castable: true,
    reading: { app_id: null, display_name: null, connected: true },
    ...over,
  };
}

function notice(over: Partial<CastNotice> = {}): CastNotice {
  return {
    id: "n1",
    sender: "owner",
    title: "Dinner",
    body: "",
    seconds: 30,
    created_at: new Date().toISOString(),
    stopped_at: null,
    tvs: [
      { tv_id: "a", name: "Big TV", outcome: "shown", why: "Ambient" },
      { tv_id: "b", name: "Parents", outcome: "woke", why: "was off" },
      { tv_id: "c", name: "Twins room", outcome: "skipped", why: "Netflix open" },
    ],
    ...over,
  };
}

describe("a TV in words", () => {
  it("says every state with a word, never only a colour", () => {
    expect(tvWords(tv())).toEqual({ label: "Ready", sub: "Ambient", tone: "ready" });
    expect(tvWords(tv({ why: "nothing open" })).sub).toBe("Home screen, nothing playing");
    expect(tvWords(tv({ state: "standby", why: "off" }))).toMatchObject({
      label: "Off",
      tone: "off",
    });
    expect(tvWords(tv({ state: "busy", why: "Netflix · playing", castable: false }))).toEqual({
      label: "In use",
      sub: "Netflix · playing",
      tone: "busy",
    });
    expect(tvWords(tv({ state: "unknown", why: "not connected" }))).toMatchObject({
      label: "Not answering",
      sub: "Not reachable right now",
    });
    expect(tvWords(tv({ state: "unknown", why: "no status yet" })).sub).toBe("Still connecting");
  });

  // The spike's finding 6: our own card reads busy to the gate, which is right, but the owner
  // should read that it is showing a notice.
  it("names our own notice rather than calling the TV busy", () => {
    const ours = tv({
      state: "busy",
      castable: false,
      why: "Default Media Receiver · paused",
      reading: { app_id: MEDIA_RECEIVER, display_name: "Default Media Receiver", connected: true },
    });
    expect(tvWords(ours).sub).toBe("Showing a notice");
  });

  it("counts the TVs that would take a notice now, and says why the rest won't", () => {
    const busy = tv({
      id: "t2",
      name: "Twins room",
      state: "busy",
      castable: false,
      why: "Netflix open",
    });
    expect(readyCount([tv(), tv({ state: "standby", why: "off" }), busy])).toBe(2);
    expect(skipReason(tv())).toBeNull();
    expect(skipReason(busy)).toBe("Twins room is netflix open and will be skipped.");
  });
});

describe("the result", () => {
  it("titles it by how many TVs took it", () => {
    expect(resultTitle(notice())).toBe("Cast to 2 of 3");
    expect(
      resultTitle(notice({ tvs: [{ tv_id: "c", name: "T", outcome: "skipped", why: "x" }] })),
    ).toBe("Not cast");
    expect(
      resultTitle(
        notice({
          tvs: [
            { tv_id: "c", name: "T", outcome: "skipped", why: "x" },
            { tv_id: "d", name: "U", outcome: "failed", why: "y" },
          ],
        }),
      ),
    ).toBe("Cast to none of 2");
  });

  it("says each outcome, and that a woken TV stays on", () => {
    expect(outcomeLabel("shown")).toBe("On screen");
    expect(outcomeLabel("woke")).toBe("Turned on");
    expect(outcomeLabel("skipped")).toBe("Skipped");
    expect(outcomeLabel("failed")).toBe("Didn't cast");
    expect(outcomeSub({ outcome: "woke", why: "was off" })).toMatch(/stays on/);
    expect(outcomeSub({ outcome: "shown", why: "Ambient" })).toBe("Over ambient");
    expect(outcomeSub({ outcome: "shown", why: "" })).toBe("");
    expect(outcomeSub({ outcome: "skipped", why: "not on the network" })).toBe(
      "Not on the network",
    );
  });

  it("offers Take it down only while something is still up", () => {
    const now = Date.parse("2026-10-15T03:00:00Z");
    const at = new Date(now - 10_000).toISOString();
    expect(canTakeDown(notice({ created_at: at }), now)).toBe(true);
    expect(canTakeDown(notice({ created_at: at, stopped_at: at }), now)).toBe(false);
    expect(canTakeDown(notice({ created_at: new Date(now - 31_000).toISOString() }), now)).toBe(
      false,
    );
    expect(
      canTakeDown(
        notice({ created_at: at, tvs: [{ tv_id: "c", name: "T", outcome: "skipped", why: "" }] }),
        now,
      ),
    ).toBe(false);
  });

  it("sums a send up for Sent, naming who sent it unless it was the owner", () => {
    const n = notice({ created_at: "2026-10-15T18:42:00Z" });
    expect(sentSummary(n, "UTC")).toMatch(/^18:42 · Big TV, Parents · 1 skipped$/);
    expect(sentSummary(notice({ sender: "debug", created_at: n.created_at }), "UTC")).toMatch(
      /from a jbrain test/,
    );
    expect(sentSummary(notice({ sender: "jerv", created_at: n.created_at }), "UTC")).toMatch(
      /from Jerv/,
    );
    expect(
      sentSummary(
        notice({
          sender: "workflow:Morning",
          created_at: n.created_at,
          tvs: [{ tv_id: "c", name: "T", outcome: "skipped", why: "" }],
        }),
        "UTC",
      ),
    ).toMatch(/from Morning · on no TV · 1 skipped/);
  });
});

describe("helpers", () => {
  it("says seconds as a person would", () => {
    expect(secondsLabel(30)).toBe("30 s");
    expect(secondsLabel(120)).toBe("2 min");
  });
});

describe("the composer", () => {
  const photo = { source: {} as CanvasImageSource, w: 4000, h: 3000 };

  it("needs a photo or words, and names the notice by its words", () => {
    const d = emptyCastDraft();
    expect(draftReady(d)).toBe(false);
    expect(draftReady({ ...d, words: "  " })).toBe(false);
    expect(draftReady({ ...d, photo })).toBe(true);
    expect(draftTitle({ ...d, photo })).toBe("Picture");
    expect(draftTitle({ ...d, words: " Dinner\n is   ready " })).toBe("Dinner is ready");
  });

  it("moves the words when placing them, and holds them on the screen", () => {
    const d = { ...emptyCastDraft(), words: "Hi" };
    expect(dragDraft(d, "words", 100, -50).pos).toEqual({ x: 1060, y: 490 });
    expect(dragDraft(d, "words", 5000, 5000).pos).toEqual({ x: TV_W, y: TV_H });
  });

  it("pans the photo, held to its edges, and leaves a draft with no photo alone", () => {
    const d = { ...emptyCastDraft(), photo };
    // 4000x3000 covering 1920x1080 is 1920x1440: 180 px of slack above and below, none sideways.
    const moved = dragDraft(d, "photo", 300, 500);
    expect(moved.panX).toBe(0);
    expect(moved.panY).toBe(180);
    const none = emptyCastDraft();
    expect(dragDraft(none, "photo", 10, 10)).toBe(none);
    // Words that are empty can't be placed, so the drag pans the photo instead.
    expect(dragDraft(d, "words", 0, -500).panY).toBe(-180);
  });

  it("draws the photo first and the words over it", () => {
    const calls: string[] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (_o, k) =>
        k === "measureText"
          ? (t: string) => ({ width: t.length * 50 })
          : (...a: unknown[]) => calls.push(`${String(k)}${k === "fillText" ? `:${a[0]}` : ""}`),
      set: () => true,
    }) as unknown as CanvasRenderingContext2D;
    drawFrame(ctx, { ...emptyCastDraft(), photo, words: "Dinner", style: "dark" });
    const image = calls.indexOf("drawImage");
    const plate = calls.indexOf("roundRect");
    const words = calls.indexOf("fillText:Dinner");
    expect(image).toBeGreaterThan(-1);
    expect(plate).toBeGreaterThan(image);
    expect(words).toBeGreaterThan(plate);
  });
});
