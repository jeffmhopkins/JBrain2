import { describe, expect, it } from "vitest";
import type { CastNotice, CastTv } from "./api/client";
import {
  LAYERS_MAX,
  MEDIA_RECEIVER,
  SIZE_MAX,
  SIZE_MIN,
  TV_H,
  TV_W,
  ZOOM_MAX,
  ZOOM_MIN,
  addPicture,
  addWords,
  canAdd,
  canTakeDown,
  coverZoom,
  draftReady,
  draftTitle,
  drawFrame,
  drawOrder,
  emptyCastDraft,
  fitNote,
  fitWords,
  handleAt,
  inside,
  layerAt,
  layerById,
  layerScale,
  moveLayer,
  onScreenSeconds,
  outcomeLabel,
  outcomeSub,
  pictureRect,
  readyCount,
  removeLayer,
  resultTitle,
  scaleLayer,
  secondsLabel,
  sentSummary,
  setStyle,
  setWords,
  settleTurn,
  skipReason,
  turnLayer,
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

describe("time on screen", () => {
  it("reads seconds or minutes, and says when it's outside what the box takes", () => {
    expect(onScreenSeconds("45", "s")).toEqual({ seconds: 45 });
    expect(onScreenSeconds("1.5", "min")).toEqual({ seconds: 90 });
    expect(onScreenSeconds("5", "min")).toEqual({ seconds: 300 });
    expect(onScreenSeconds("6", "min")).toHaveProperty("problem");
    expect(onScreenSeconds("3", "s")).toHaveProperty("problem");
    expect(onScreenSeconds("", "s")).toEqual({ problem: "Give it a time." });
    expect(onScreenSeconds("abc", "s")).toEqual({ problem: "Give it a time." });
  });
});

describe("the composer", () => {
  const photo = { source: {} as CanvasImageSource, w: 4000, h: 3000 };

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

  it("needs a picture or words, and names the notice by its words", () => {
    const d = emptyCastDraft();
    expect(draftReady(d)).toBe(false);
    expect(draftReady(addWords(d, "  "))).toBe(false);
    expect(draftReady(addPicture(d, photo))).toBe(true);
    expect(draftTitle(addPicture(d, photo))).toBe("Picture");
    const two = addWords(addWords(d, " Dinner\n is   ready "), "Come down");
    expect(draftTitle(two)).toBe("Dinner is ready · Come down");
    expect(new TextEncoder().encode(draftTitle(addWords(d, "é".repeat(200)))).length).toBe(120);
  });

  it("adds things centred, words a step apart, the first picture filling the screen", () => {
    let d = addWords(emptyCastDraft(), "One");
    d = addWords(d, "Two");
    d = addPicture(d, photo);
    d = addPicture(d, photo);
    const [one, two, first, second] = d.layers;
    expect(one).toMatchObject({ id: 1, x: 960, y: 540, size: 140, turn: 0 });
    expect(two?.y).toBeGreaterThan(540);
    // 4000x3000 fits the screen at 1440x1080; covering it takes 1920 wide, 4/3 as big.
    expect(first).toMatchObject({ kind: "picture", zoom: coverZoom(photo) });
    expect(coverZoom(photo)).toBeCloseTo(4 / 3);
    expect(pictureRect(first as never)).toMatchObject({ x: 0, w: 1920 });
    expect(second).toMatchObject({ zoom: 0.5 });
    expect(d.nextId).toBe(5);
    expect(layerById(d, 3)).toBe(first);
    expect(layerById(d, null)).toBeNull();
  });

  it("holds no more than a notice can carry", () => {
    let d = emptyCastDraft();
    for (let i = 0; i < LAYERS_MAX + 2; i++) d = addWords(d, `w${i}`);
    expect(d.layers).toHaveLength(LAYERS_MAX);
    expect(canAdd(d)).toBe(false);
    expect(addPicture(d, photo)).toBe(d);
  });

  it("draws pictures under words, whatever order they were added in", () => {
    const d = addPicture(addWords(emptyCastDraft(), "Hi"), photo);
    expect(drawOrder(d).map((l) => l.kind)).toEqual(["picture", "words"]);
    const calls: string[] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (_o, k) =>
        k === "measureText"
          ? (t: string) => ({ width: t.length * 50 })
          : (...a: unknown[]) => calls.push(`${String(k)}${k === "fillText" ? `:${a[0]}` : ""}`),
      set: () => true,
    }) as unknown as CanvasRenderingContext2D;
    drawFrame(ctx, setStyle(d, 1, "dark"));
    const image = calls.indexOf("drawImage");
    const plate = calls.indexOf("roundRect");
    const words = calls.indexOf("fillText:Hi");
    expect(image).toBeGreaterThan(-1);
    expect(plate).toBeGreaterThan(image);
    expect(words).toBeGreaterThan(plate);
  });

  it("changes the words, and drops a layer emptied of them", () => {
    const d = addWords(emptyCastDraft(), "Hi");
    expect(setWords(d, 1, "Hello").layers[0]).toMatchObject({ text: "Hello" });
    expect(setWords(d, 1, "  ").layers).toHaveLength(0);
    expect(removeLayer(d, 1).layers).toHaveLength(0);
    // Asking for a change that changes nothing hands the same draft back.
    expect(setStyle(addPicture(d, photo), 2, "dark").layers[1]).not.toHaveProperty("style");
  });

  it("moves a layer, its centre held on the screen", () => {
    const d = addWords(emptyCastDraft(), "Hi");
    expect(moveLayer(d, 1, 100, -50).layers[0]).toMatchObject({ x: 1060, y: 490 });
    expect(moveLayer(d, 1, 5000, 5000).layers[0]).toMatchObject({ x: TV_W, y: TV_H });
    expect(moveLayer(d, 9, 10, 10)).toBe(d);
  });

  it("resizes each layer within its range", () => {
    const d = addPicture(addWords(emptyCastDraft(), "Hi"), photo);
    expect(scaleLayer(d, 2, 9).layers[1]).toMatchObject({ zoom: ZOOM_MAX });
    expect(scaleLayer(d, 2, 0.01).layers[1]).toMatchObject({ zoom: ZOOM_MIN });
    expect(scaleLayer(d, 1, 1000).layers[0]).toMatchObject({ size: SIZE_MAX });
    expect(scaleLayer(d, 1, 100.4).layers[0]).toMatchObject({ size: 100 });
    expect(layerScale(d.layers[0] as never)).toBe(140);
  });

  it("turns a layer, kept within a revolution and settled square near a quarter", () => {
    const deg = (n: number) => (n * Math.PI) / 180;
    expect(settleTurn(deg(30))).toBeCloseTo(deg(30));
    expect(settleTurn(deg(93))).toBe(Math.PI / 2);
    expect(settleTurn(deg(-2))).toBe(0);
    expect(settleTurn(deg(400))).toBeCloseTo(deg(40));
    expect(settleTurn(deg(-182))).toBe(Math.PI);
    const d = addWords(emptyCastDraft(), "Hi");
    expect(turnLayer(d, 1, deg(45)).layers[0]?.turn).toBeCloseTo(deg(45));
  });

  it("a touch takes the topmost layer as drawn: words over pictures, else nothing", () => {
    const ctx = measuring();
    // "Hi" at 140 px is a 229.6 x 185.6 box on the centre.
    const d = addPicture(addWords(emptyCastDraft(), "Hi"), { ...photo, w: 1920, h: 1080 });
    const small = scaleLayer(d, 2, 0.5);
    expect(layerAt(ctx, small, { x: 960, y: 540 })).toBe(1);
    expect(layerAt(ctx, small, { x: 1300, y: 540 })).toBe(2);
    expect(layerAt(ctx, small, { x: 10, y: 10 })).toBeNull();
    // A finger's slack around the words still takes them, but not around a picture.
    expect(layerAt(ctx, addWords(emptyCastDraft(), "Hi"), { x: 1090, y: 540 }, 20)).toBe(1);
  });

  it("a turned layer is touched, and its handle found, where it is drawn", () => {
    // A 400x100 bar on the screen's centre, turned a quarter: it now stands 100 wide, 400 tall.
    const bar = { x: 760, y: 490, w: 400, h: 100, turn: Math.PI / 2 };
    expect(inside(bar, { x: 960, y: 700 })).toBe(true);
    expect(inside(bar, { x: 1120, y: 540 })).toBe(false);
    const h = handleAt(bar);
    expect(h.x).toBeCloseTo(910);
    expect(h.y).toBeCloseTo(740);
    expect(turnPoint({ x: 1, y: 0 }, { x: 0, y: 0 }, Math.PI / 2).y).toBeCloseTo(1);
  });

  it("draws turned layers about their own centres, each in its own save", () => {
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
    let d = addPicture(addWords(emptyCastDraft(), "Hi"), photo);
    d = turnLayer(turnLayer(d, 2, 0.5), 1, -0.25);
    drawFrame(ctx, moveLayer(d, 1, 100, 0));
    expect(calls.filter((c) => c.startsWith("rotate"))).toEqual(["rotate(0.5)", "rotate(-0.25)"]);
    expect(calls.filter((c) => c.startsWith("save")).length).toBe(2);
    expect(calls.filter((c) => c.startsWith("restore")).length).toBe(2);
    expect(calls).toContain("translate(960,540)");
    expect(calls).toContain("translate(1060,540)");
  });

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
    expect(fitNote(undefined, 140)).toBeNull();
  });
});
