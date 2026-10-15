import { describe, expect, it } from "vitest";
import type { CastNotice, CastTv } from "./api/client";
import {
  MEDIA_RECEIVER,
  SIZE_MAX,
  SIZE_MIN,
  TV_H,
  TV_W,
  ZOOM_MAX,
  ZOOM_MIN,
  canTakeDown,
  castPhotoPlacement,
  draftReady,
  draftTitle,
  dragDraft,
  drawFrame,
  emptyCastDraft,
  fitNote,
  fitWords,
  handleAt,
  outcomeLabel,
  outcomeSub,
  partAt,
  partScale,
  partTurn,
  photoRect,
  quarterTurn,
  readyCount,
  resultTitle,
  scaleDraft,
  secondsLabel,
  sentSummary,
  skipReason,
  turnDraft,
  turnPoint,
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
    // Words that are empty can't be moved, and a drag on them leaves the photo where it is.
    expect(dragDraft(d, "words", 0, -500)).toBe(d);
  });

  it("lets a photo shrink below the screen and go anywhere on it, wholly on screen", () => {
    const d = scaleDraft({ ...emptyCastDraft(), photo }, "photo", 0.5);
    // Half of 1920x1440 is 960x720: 480 px of room each side, 180 above and below.
    expect(castPhotoPlacement(photo, d)).toMatchObject({ w: 960, h: 720, x: 480, y: 180 });
    const corner = dragDraft(d, "photo", -5000, 5000);
    expect(castPhotoPlacement(photo, corner)).toMatchObject({ x: 0, y: 360 });
    expect(photoRect(corner)).toEqual({ x: 0, y: 360, w: 960, h: 720 });
    // Growing it back past the screen re-holds the pan so no gap shows at its edge.
    const big = scaleDraft(corner, "photo", 1);
    expect(big.panX).toBeCloseTo(0);
    expect(big.panY).toBe(180);
    expect(photoRect(big)).toEqual({ x: 0, y: 0, w: TV_W, h: TV_H });
  });

  it("resizes each part within its slider's range", () => {
    const d = { ...emptyCastDraft(), photo, words: "Hi" };
    expect(scaleDraft(d, "photo", 9).zoom).toBe(ZOOM_MAX);
    expect(scaleDraft(d, "photo", 0.01).zoom).toBe(ZOOM_MIN);
    expect(scaleDraft(d, "words", 1000).size).toBe(SIZE_MAX);
    expect(scaleDraft(d, "words", 100.4).size).toBe(100);
    expect(partScale(d, "words")).toBe(140);
    expect(partScale(d, "photo")).toBe(1);
    expect(scaleDraft(emptyCastDraft(), "photo", 2)).toEqual(emptyCastDraft());
  });

  it("a touch takes the words where they sit over the photo, else the photo, else nothing", () => {
    const words = { x: 100, y: 100, w: 200, h: 100 };
    const whole = { x: 0, y: 0, w: TV_W, h: TV_H };
    expect(partAt(words, whole, { x: 150, y: 150 })).toBe("words");
    expect(partAt(words, whole, { x: 500, y: 500 })).toBe("photo");
    expect(partAt(words, null, { x: 500, y: 500 })).toBeNull();
    // A finger's slack around the words still takes them.
    expect(partAt(words, null, { x: 310, y: 150 }, 20)).toBe("words");
    expect(partAt(null, null, { x: 1, y: 1 })).toBeNull();
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

  /** A context that measures at half the font size a letter, as a bold face roughly does. */
  function measuring(): CanvasRenderingContext2D {
    let px = 10;
    return {
      measureText: (t: string) => ({ width: t.length * px * 0.5 }),
      set font(v: string) {
        px = Number(/(\d+)px/.exec(v)?.[1]);
      },
    } as unknown as CanvasRenderingContext2D;
  }

  it("keeps the asked size when the words fit, and shrinks them until every word does", () => {
    expect(fitWords(measuring(), "Dinner", 140)).toEqual({
      lines: ["Dinner"],
      size: 140,
      whole: true,
    });
    // Five lines at 140: the wrap would drop the last, so they come down a size.
    const long = fitWords(measuring(), "word ".repeat(24).trim(), 140);
    expect(long.size).toBeLessThan(140);
    expect(long.whole).toBe(true);
    expect(long.lines.join(" ").split(" ")).toHaveLength(24);
    // Four lines of 320 px are taller than the screen even though the wrap kept them all.
    const tall = fitWords(measuring(), "aaaaaaaaa bbbbbbbbb ccccccccc ddddddddd", 320);
    expect(tall.size).toBeLessThan(320);
    expect(tall.lines.length * tall.size * 1.12).toBeLessThanOrEqual(1080);
  });

  it("says when the words were shrunk, or can't all fit even at the smallest size", () => {
    const none = fitWords(measuring(), "x ".repeat(400), 140);
    expect(none).toMatchObject({ size: SIZE_MIN, whole: false });
    expect(fitNote(none, 140)).toMatch(/end is cut off/);
    expect(fitNote({ lines: ["a"], size: 100, whole: true }, 140)).toBe(
      "Shrunk to 100 px so every word fits on the screen.",
    );
    expect(fitNote({ lines: ["a"], size: 140, whole: true }, 140)).toBeNull();
    expect(fitNote(null, 140)).toBeNull();
  });

  it("turns a part, kept within a revolution and settled square near a quarter", () => {
    const d = { ...emptyCastDraft(), photo, words: "Hi" };
    const deg = (n: number) => (n * Math.PI) / 180;
    expect(turnDraft(d, "photo", deg(30)).turn).toBeCloseTo(deg(30));
    expect(turnDraft(d, "photo", deg(93)).turn).toBe(Math.PI / 2);
    expect(turnDraft(d, "photo", deg(-2)).turn).toBe(0);
    expect(turnDraft(d, "words", deg(400)).wordsTurn).toBeCloseTo(deg(40));
    expect(turnDraft(d, "words", deg(-182)).wordsTurn).toBe(Math.PI);
    expect(partTurn(turnDraft(d, "words", deg(45)), "words")).toBeCloseTo(deg(45));
    // Nothing to turn is left alone.
    const none = emptyCastDraft();
    expect(turnDraft(none, "photo", 1)).toBe(none);
    expect(turnDraft(none, "words", 1)).toBe(none);
    // The button goes to the next quarter from the nearest one.
    expect(quarterTurn(turnDraft(d, "photo", deg(30)), "photo").turn).toBe(Math.PI / 2);
    expect(quarterTurn(turnDraft(d, "photo", Math.PI), "photo").turn).toBe(-Math.PI / 2);
  });

  it("a turned part is touched, and its handle found, where it is drawn", () => {
    // A 400x100 bar on the screen's centre, turned a quarter: it now stands 100 wide, 400 tall.
    const bar = { x: 760, y: 490, w: 400, h: 100, turn: Math.PI / 2 };
    expect(partAt(bar, null, { x: 960, y: 700 })).toBe("words");
    expect(partAt(bar, null, { x: 1120, y: 540 })).toBeNull();
    const h = handleAt(bar);
    expect(h.x).toBeCloseTo(910);
    expect(h.y).toBeCloseTo(740);
    expect(turnPoint({ x: 1, y: 0 }, { x: 0, y: 0 }, Math.PI / 2).y).toBeCloseTo(1);
  });

  it("draws a turned photo and turned words about their own centres", () => {
    const calls: string[] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (_o, k) =>
        k === "measureText"
          ? (t: string) => ({ width: t.length * 50 })
          : (...a: unknown[]) =>
              calls.push(
                `${String(k)}(${a.map((n) => Math.round(Number(n) * 100) / 100).join(",")})`,
              ),
      set: () => true,
    }) as unknown as CanvasRenderingContext2D;
    drawFrame(ctx, { ...emptyCastDraft(), photo, turn: 0.5, words: "Hi", wordsTurn: -0.25 });
    const turns = calls.filter((c) => c.startsWith("rotate"));
    expect(turns).toEqual(["rotate(0.5)", "rotate(-0.25)"]);
    // Each turn is wrapped in its own save and restore, so the words don't inherit the photo's.
    expect(calls.filter((c) => c.startsWith("save")).length).toBe(2);
    expect(calls.filter((c) => c.startsWith("restore")).length).toBe(2);
    expect(calls).toContain("translate(960,540)");
  });
});
