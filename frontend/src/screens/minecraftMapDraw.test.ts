import { describe, expect, it, vi } from "vitest";
import type { BaseLayer, TileSpot } from "../minecraftMap";
import { mcMapMarkers } from "../minecraftMapFixtures";
import {
  type TileEntry,
  type Tokens,
  bedGlyph,
  drawTiles,
  drawWorldMarkers,
  gateGlyph,
  placeLabels,
  respawnCurve,
  scanSquare,
} from "./minecraftMapDraw";

// Every canvas call is a no-op spy; only what reaches drawImage is under test.
function fakeCtx() {
  const calls: CanvasImageSource[] = [];
  const ctx = new Proxy(
    {},
    {
      get: (_t, prop) =>
        prop === "drawImage"
          ? (img: CanvasImageSource) => calls.push(img)
          : typeof prop === "string"
            ? vi.fn()
            : undefined,
      set: () => true,
    },
  ) as CanvasRenderingContext2D;
  return { ctx, calls };
}

const T = { s2: "#000", border: "#111" } as Tokens;
const spot = (tx: number): TileSpot => ({ zoom: 0, tx, tz: 0, sx: tx * 256, sy: 0, size: 256 });
const img = (name: string) => ({ name }) as unknown as CanvasImageSource;

describe("drawing the base layer's tiles", () => {
  it("shows the Biomes tile under a Surface tile that isn't here yet, asking for it first", () => {
    const { ctx, calls } = fakeCtx();
    const cache = new Map<string, TileEntry>([
      ["biomes|0", { img: img("biomes0"), at: 0, state: "ok" }],
      ["surface|1", { img: img("surface1"), at: 0, state: "ok" }],
    ]);
    const asked: string[] = [];
    const key = (s: TileSpot, layer: BaseLayer) => `${layer}|${s.tx}`;
    const pending = drawTiles(
      ctx,
      [spot(0), spot(1), spot(2)],
      "surface",
      (s, layer) => cache.get(key(s, layer)),
      (s, layer) => {
        asked.push(key(s, layer));
        return cache.get(key(s, layer)) ?? { img: null, at: 0, state: "loading" };
      },
      T,
    );
    // Biomes first for every spot Surface hasn't drawn, then Surface everywhere.
    expect(asked).toEqual(["biomes|0", "biomes|2", "surface|0", "surface|1", "surface|2"]);
    // Spot 0 shows its biomes while Surface loads; spot 1 its surface; spot 2 is a hole.
    expect(calls).toEqual([img("biomes0"), img("surface1")]);
    expect(pending).toBe(2); // Surface is still on its way at spots 0 and 2
  });

  it("asks for nothing else on the Biomes layer", () => {
    const { ctx, calls } = fakeCtx();
    const asked: string[] = [];
    drawTiles(
      ctx,
      [spot(0)],
      "biomes",
      () => undefined,
      (_s, layer) => {
        asked.push(layer);
        return { img: img("b"), at: 0, state: "ok" };
      },
      T,
    );
    expect(asked).toEqual(["biomes"]);
    expect(calls).toHaveLength(1);
  });
});

// ---- beacons and gates (§M26) ----

/** A context that records every call, so the drawing order can be read back. */
function recordingCtx() {
  const log: [string, unknown[]][] = [];
  const state: Record<string, unknown> = {};
  const ctx = new Proxy(state, {
    get: (t, prop: string) =>
      prop in t
        ? t[prop]
        : prop === "measureText"
          ? (s: string) => ({ width: s.length * 6 })
          : (...args: unknown[]) => {
              log.push([prop, args]);
            },
    set: (t, prop: string, v) => {
      t[prop] = v;
      log.push([`=${prop}`, [v]]);
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
  return { ctx, log };
}

const TOK = {
  halo: "halo",
  ink: "ink",
  inkOn: "inkOn",
  plate: "plate",
  text: "text",
  green: "green",
  violet: "violet",
} as Tokens;
const FRAME = { w: 390, h: 700, bottom: 150, top: 64 };
const VIEW = { x: -60, z: -60, s: 1.25 };

describe("drawing beacons and gates", () => {
  const marks = mcMapMarkers("slot2");
  const base = {
    beacons: marks.beacons,
    gates: marks.gates,
    dim: "overworld" as const,
    showBeacons: true,
    showGates: true,
    selBeacon: null,
    selGate: null,
  };

  it("draws the squares, then the gates' lines, then their places — under every tile", () => {
    const { ctx, log } = recordingCtx();
    const out = drawWorldMarkers(ctx, VIEW, FRAME, TOK, base);
    const squares = log.flatMap(([k], i) => (k === "strokeRect" ? [i] : []));
    const dotted = log.findIndex(
      ([k, a]) => k === "setLineDash" && JSON.stringify(a) === "[[1,6]]",
    );
    const pin = log.findIndex(([k, a]) => k === "arc" && a[2] === 12);
    // Three beacons, each outlined over a halo.
    expect(squares).toHaveLength(6);
    expect(Math.max(...squares)).toBeLessThan(dotted);
    expect(dotted).toBeLessThan(pin);
    // No tile is drawn here: they're handed back to cluster, the open, the dormant and the
    // Nether gate with every beacon.
    expect(log.some(([k]) => k === "roundRect")).toBe(false);
    expect(out.markers.map((m) => m.type).sort()).toEqual([
      "beacon",
      "beacon",
      "beacon",
      "gate",
      "gate",
      "gate",
    ]);
    // One line (gate 1 to the Tree farm), one pin, and its label and the Nether gate's.
    expect(out.avoid.segs).toHaveLength(1);
    expect(out.pins.map((p) => p.g.id)).toEqual([1]);
    expect(out.labels.map((l) => [l.txt, l.own, l.side])).toEqual([
      ["Tree farm", "g:1", 12],
      ["to Nether base · in the Nether", "g:3", 12],
    ]);
  });

  it("fills only the tapped square, and keeps the tapped gate apart for its ring", () => {
    const { ctx, log } = recordingCtx();
    const out = drawWorldMarkers(ctx, VIEW, FRAME, TOK, {
      ...base,
      selBeacon: "overworld@60,72,-40",
      selGate: 1,
    });
    expect(log.filter(([k]) => k === "fillRect")).toHaveLength(1);
    expect(out.tapped?.type).toBe("gate");
    expect(out.markers.filter((m) => m.type === "gate")).toHaveLength(2);
    expect(out.labels.find((l) => l.own === "g:1")?.pr).toBe(-1); // the tapped one's first
  });

  it("draws nothing for a switched-off layer or another dimension", () => {
    const { ctx, log } = recordingCtx();
    const out = drawWorldMarkers(ctx, VIEW, FRAME, TOK, {
      ...base,
      showBeacons: false,
      dim: "nether",
    });
    expect(log).toEqual([]);
    expect(out.markers).toEqual([]);
  });

  it("a dormant gate is the same portal in a dashed ring; an open one is filled", () => {
    vi.stubGlobal("Path2D", class {});
    const open = recordingCtx();
    gateGlyph(open.ctx, 50, 50, true, TOK, false);
    const dorm = recordingCtx();
    gateGlyph(dorm.ctx, 50, 50, false, TOK, false);
    const dashed = (log: [string, unknown[]][]) =>
      log.some(([k, a]) => k === "setLineDash" && JSON.stringify(a) === "[[3,2.5]]");
    expect(dashed(open.log)).toBe(false);
    expect(dashed(dorm.log)).toBe(true);
    // Both draw the frame and the swirl; only the open one fills the frame.
    expect(open.log.filter(([k]) => k === "fill")).toHaveLength(2); // halo + portal
    expect(dorm.log.filter(([k]) => k === "fill")).toHaveLength(1); // halo only
    expect(dorm.log.filter(([k]) => k === "stroke")).toHaveLength(3); // ring, frame, swirl
    vi.unstubAllGlobals();
  });
});

describe("beds and the respawn curve (§M8)", () => {
  it("a bed is its player's ring, dashed only when no bed was seen at the point", () => {
    vi.stubGlobal("Path2D", class {});
    const dashed = (log: [string, unknown[]][]) =>
      log.some(([k, a]) => k === "setLineDash" && JSON.stringify(a) === "[[3,2.5]]");
    const bed = recordingCtx();
    bedGlyph(bed.ctx, 50, 50, "bed", "#7fa7c9", TOK, false);
    const unseen = recordingCtx();
    bedGlyph(unseen.ctx, 50, 50, "other", "#7fa7c9", TOK, true);
    expect(dashed(bed.log)).toBe(false);
    expect(dashed(unseen.log)).toBe(true);
    // The tapped one carries the selection ring.
    expect(unseen.log.some(([k, a]) => k === "arc" && a[2] === 18)).toBe(true);
    expect(bed.log.some(([k, a]) => k === "arc" && a[2] === 18)).toBe(false);
    vi.unstubAllGlobals();
  });

  it("curves from the skull to the waking with an arrowhead, clear of both ends", () => {
    const { ctx, log } = recordingCtx();
    const segs = respawnCurve(ctx, 0, 200, 200, 0, "#7fa7c9", TOK);
    expect(segs.length).toBeGreaterThan(4);
    // It bows off the straight line between the ends: a jump, not a path.
    const mid = segs[Math.floor(segs.length / 2)];
    expect(Math.abs((mid?.ax ?? 0) + (mid?.ay ?? 0) - 200)).toBeGreaterThan(20);
    // Halo then colour, twice: the curve and its arrowhead.
    expect(log.filter(([k]) => k === "stroke")).toHaveLength(4);
    const first = segs[0];
    expect(Math.hypot(first?.ax ?? 0, (first?.ay ?? 0) - 200)).toBeGreaterThan(10);
    // Ends that touch draw nothing.
    const near = recordingCtx();
    expect(respawnCurve(near.ctx, 0, 0, 20, 20, "#7fa7c9", TOK)).toEqual([]);
    expect(near.log).toEqual([]);
  });
});

describe("placing labels round beacons and gates", () => {
  const plates = (log: [string, unknown[]][]) =>
    log.filter(([k, a]) => k === "roundRect" && a[4] === 6).map(([, a]) => a.slice(0, 2));

  it("drops a label that would sit on another gate's line, and keeps one on its own", () => {
    const { ctx, log } = recordingCtx();
    const avoid = {
      rects: [],
      // Another gate's line straight through every slot round the marker at (200, 300).
      segs: [
        { ax: 0, ay: 300, bx: 390, by: 300, own: "g:2" },
        { ax: 200, ay: 0, bx: 200, by: 600, own: "g:2" },
      ],
    };
    placeLabels(
      ctx,
      [
        { txt: "Tree farm", x: 200, y: 300, pr: 2, own: "g:1", side: 12 },
        { txt: "Mine", x: 200, y: 300, pr: 2, own: "g:2", side: 12 },
      ],
      [],
      FRAME,
      TOK,
      avoid,
    );
    expect(plates(log)).toEqual([[216, 290]]); // only g:2's own label, on its own line
  });

  it("moves a player's label off a gate tile, and never under a disc", () => {
    const { ctx, log } = recordingCtx();
    placeLabels(ctx, [{ txt: "BlockyFox", x: 100, y: 200, pr: 0, own: "p:1" }], [], FRAME, TOK, {
      rects: [{ x: 90, y: 160, w: 24, h: 24, own: "g:1" }],
      segs: [],
    });
    // Above (y 174) would cover the gate's tile; it goes below its disc instead.
    expect(plates(log)).toEqual([[67, 230]]);
  });
});

describe("a live scan on the map (§M25)", () => {
  const texts = (log: [string, unknown[]][]) =>
    log.filter(([k]) => k === "fillText").map(([, a]) => a[0]);

  it("draws the 33 × 33 blocks the pack searches, to the block, washed only when found", () => {
    const v = { x: 0, z: 0, s: 2 };
    const f = { w: 400, h: 400, bottom: 0, top: 0 };
    const found = recordingCtx();
    const r = scanSquare(found.ctx, v, f, { x: 32.7, z: -70.2 }, "steel", true, TOK);
    // Floored to the block they stand in (32, -71), 16 each way and the block itself.
    expect(r.w).toBe(33 * 2);
    expect(r.h).toBe(33 * 2);
    expect([r.x, r.y]).toEqual([200 + (32 - 16) * 2, 200 + (-71 - 16) * 2]);
    expect(found.log.filter(([k]) => k === "fillRect")).toHaveLength(1);
    expect(found.log.some(([k, a]) => k === "=globalAlpha" && a[0] === 0.1)).toBe(true);
    // A hairline and the viewfinder corners: never a dash, so never a beacon's square.
    expect(found.log.some(([k]) => k === "setLineDash")).toBe(false);
    const none = recordingCtx();
    scanSquare(none.ctx, v, f, { x: 0, z: 0 }, "steel", false, TOK);
    expect(none.log.filter(([k]) => k === "fillRect")).toHaveLength(0);
  });

  it("a scanner's plate falls back to their name rather than losing it", () => {
    const { ctx, log } = recordingCtx();
    // Room for "BlockyFox" between two tiles, not for the whole plate.
    const avoid = {
      rects: [
        { x: 40, y: 150, w: 24, h: 60, own: "g:1" },
        { x: 136, y: 150, w: 24, h: 60, own: "g:2" },
        { x: 40, y: 210, w: 120, h: 40, own: "g:3" },
      ],
      segs: [],
    };
    placeLabels(
      ctx,
      [
        {
          txt: "BlockyFox · Scanning: diamond",
          short: "BlockyFox",
          x: 100,
          y: 200,
          pr: 0,
          own: "p:1",
        },
      ],
      [],
      FRAME,
      TOK,
      avoid,
    );
    expect(texts(log)).toEqual(["BlockyFox"]);
    const open = recordingCtx();
    placeLabels(
      open.ctx,
      [{ txt: "BlockyFox · Scanning: diamond", short: "BlockyFox", x: 100, y: 200, pr: 0 }],
      [],
      FRAME,
      TOK,
    );
    expect(texts(open.log)).toEqual(["BlockyFox · Scanning: diamond"]);
  });
});
