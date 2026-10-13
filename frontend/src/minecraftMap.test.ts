import { describe, expect, it } from "vitest";
import {
  type MinecraftMapPlayer,
  type MinecraftSlot,
  type MinecraftTimeline,
  minecraftTileUrl,
} from "./api/client";
import {
  type Frame,
  S_MAX,
  S_MIN,
  agoWords,
  clusterMarkers,
  clusterTarget,
  entryLine,
  fitView,
  hitTest,
  overworldSide,
  panBy,
  scaleBar,
  scaleOfZoom,
  scanPlate,
  scanWords,
  scanYRange,
  slotLine,
  spotText,
  statusLine,
  structureInfo,
  tileKey,
  tilesNear,
  toScreen,
  toWorld,
  trailBehind,
  trailSince,
  trailTail,
  tricorderLine,
  visibleTiles,
  zoomAt,
  zoomFor,
} from "./minecraftMap";

const F: Frame = { w: 390, h: 844, bottom: 120, top: 64 };

describe("map geometry", () => {
  it("puts the view's point in the middle of what the panel leaves, north up", () => {
    const v = { x: 100, z: -50, s: 2 };
    const [sx, sy] = toScreen(v, F, 100, -50);
    expect(sx).toBe(195);
    expect(sy).toBe((844 - 120) / 2 + 32);
    // East is right and south (+Z) is down.
    const [ex, ey] = toScreen(v, F, 110, -40);
    expect([ex - sx, ey - sy]).toEqual([20, 20]);
    expect(toWorld(v, F, ex, ey)).toEqual([110, -40]);
  });

  it("zooms about the finger, clamped from 1 px per chunk to 8 px per block", () => {
    const v = { x: 0, z: 0, s: 1 };
    const at = toWorld(v, F, 50, 200);
    const zoomed = zoomAt(v, F, 2, 50, 200);
    expect(zoomed.s).toBe(2);
    const after = toWorld(zoomed, F, 50, 200);
    expect(after[0]).toBeCloseTo(at[0]);
    expect(after[1]).toBeCloseTo(at[1]);
    expect(zoomAt(v, F, 1000, 0, 0).s).toBe(S_MAX);
    expect(zoomAt(v, F, 1e-6, 0, 0).s).toBe(S_MIN);
  });

  it("pans opposite the drag, in blocks at the current scale", () => {
    expect(panBy({ x: 0, z: 0, s: 4 }, 40, -20)).toEqual({ x: -10, z: 5, s: 4 });
  });

  it("asks the server for coarser tiles as the map zooms out, never past zoom 4", () => {
    expect(zoomFor(8)).toBe(0);
    expect(zoomFor(1)).toBe(0);
    expect(zoomFor(0.5)).toBe(1); // 0.5 px per block: zoom 1 tiles at 1 px per px
    expect(zoomFor(0.2)).toBe(2);
    expect(zoomFor(1 / 16)).toBe(4);
    expect(zoomFor(1 / 64)).toBe(4);
  });

  it("covers the map with tiles, the middle one first", () => {
    const tiles = visibleTiles({ x: 0, z: 0, s: 1 }, { w: 390, h: 844, bottom: 0, top: 0 });
    // 390 px wide straddling x = 0 needs two columns; 844 tall straddling z = 0, four rows.
    expect(tiles).toHaveLength(2 * 4);
    expect(tiles.every((t) => t.zoom === 0 && t.size === 256)).toBe(true);
    expect([tiles[0]?.tx, tiles[0]?.tz]).toEqual([-1, -1]);
    const far = visibleTiles({ x: 0, z: 0, s: 1 / 16 }, F);
    expect(far.every((t) => t.zoom === 4 && t.size === 256)).toBe(true);
  });

  it("builds the owner tile route from the slot, dimension, zoom, tile and layer", () => {
    expect(minecraftTileUrl("slot2", "nether", 3, -1, 4)).toBe(
      "/api/minecraft/map/tile/nether/3/-1/4.png?slot=slot2&layer=surface",
    );
    expect(minecraftTileUrl("slot2", "nether", 3, -1, 4, "biomes")).toMatch(/&layer=biomes$/);
    // Each layer's tiles are cached apart, so switching never shows the other's picture.
    const t = { zoom: 0, tx: 1, tz: 2 };
    expect(tileKey("slot1", "overworld", t)).not.toBe(tileKey("slot1", "overworld", t, "biomes"));
  });

  it("frames everyone in the visible map", () => {
    const v = fitView(
      [
        [0, 0],
        [400, 200],
      ],
      F,
    );
    expect(v).not.toBeNull();
    expect(v?.x).toBe(200);
    expect(v?.z).toBe(100);
    expect(v?.s).toBeCloseTo((390 - 112) / 400);
    expect(fitView([], F)).toBeNull();
    // One player alone doesn't zoom in past the cap.
    expect(fitView([[5, 5]], F)?.s).toBe(1.2);
  });
});

describe("markers", () => {
  const at = (sx: number, sy: number, pr = 5) => ({ x: sx, z: sy, sx, sy, pr });

  it("merges markers closer than 40 px into one count badge", () => {
    const got = clusterMarkers([at(0, 0), at(30, 0), at(20, 30), at(100, 100)]);
    expect(got.map((c) => c.members.length)).toEqual([3, 1]);
    expect(clusterMarkers([at(0, 0), at(40, 0)])).toHaveLength(2);
  });

  it("seeds clusters in priority order, so a player's or a place's marker leads", () => {
    const got = clusterMarkers([at(10, 0, 5), at(0, 0, 1)]);
    expect(got[0]?.members[0]?.pr).toBe(1);
  });

  it("a tapped badge zooms in until its members separate, or opens one on the same block", () => {
    const apart = clusterTarget(
      [
        { x: 0, z: 0 },
        { x: 100, z: 0 },
      ],
      0.1,
    );
    expect(apart?.x).toBe(50);
    expect(apart?.s).toBeCloseTo(0.52);
    const same = clusterTarget(
      [
        { x: 5, z: 5 },
        { x: 5, z: 5 },
      ],
      S_MAX,
    );
    expect(same).toBeNull();
  });

  it("picks the nearest marker within the 46-px hit circle", () => {
    const hits = [
      { sx: 100, sy: 100, id: "a" },
      { sx: 110, sy: 100, id: "b" },
    ];
    expect(hitTest(hits, 108, 100)?.id).toBe("b");
    expect(hitTest(hits, 160, 100)).toBeNull();
  });

  it("names every structure kind locate knows, and survives a new one", () => {
    expect(structureInfo("pillager_outpost").label).toBe("Pillager outpost");
    expect(structureInfo("end_city").glyph).toBe("building");
    expect(structureInfo("sky_temple").label).toBe("sky temple");
  });
});

describe("words", () => {
  it("the scale bar says when a pixel is more than a block", () => {
    expect(scaleBar(1).label).toBe("64 blocks");
    expect(scaleBar(8).label).toBe("8 blocks");
    expect(scaleBar(0.25).label).toBe("256 blocks · 1 px = 4 blocks");
    expect(scaleBar(1 / 16).label).toBe("1,024 blocks · 1 px = 1 chunk");
    expect(scaleBar(1).px).toBeGreaterThanOrEqual(56);
  });

  it("a spot reads explored with its y, or the survey with an estimated ~y", () => {
    const ex = spotText(
      { x: 1, z: 2, dimension: "overworld", source: "explored", biome: "Plains", y: 70 },
      "overworld",
    );
    expect(ex).toEqual({ title: "Plains", sub: "Explored · ground at Y 70", y: 70, survey: false });
    const sv = spotText(
      { x: 1, z: 2, dimension: "overworld", source: "survey", biome: "Cherry Grove", y: 129 },
      "overworld",
    );
    expect(sv.sub).toBe("Satellite survey (predicted) · ground at Y ~129");
    expect(sv.y).toBeNull();
    expect(spotText({ x: 0, z: 0, dimension: "the_end", source: "unknown" }, "the_end").title).toBe(
      "The void",
    );
    expect(spotText(null, "nether").title).toBe("Looking…");
  });

  it("the Nether shows the Overworld spot it leads to", () => {
    expect(overworldSide(41, -58)).toEqual([328, -464]);
  });

  const base = {
    live: true,
    loaded: true,
    online: 2,
    worldName: "Castle Hill",
    lastPlayed: null,
    unreachable: false,
    tilesVisible: 12,
    tilesPending: 0,
    dim: "overworld" as const,
    explored: true,
  };

  it("the status line says live, and how far behind the world the map is", () => {
    expect(statusLine(base)).toEqual({
      tone: "ok",
      head: "Live · 2 on",
      sub: "Up to a minute behind the world · positions live",
    });
  });

  it("the status line never says live when it isn't", () => {
    expect(statusLine({ ...base, live: false }).head).toBe("Server stopped");
    expect(statusLine({ ...base, loaded: false, live: false }).head).toBe(
      "Castle Hill isn't loaded",
    );
    const away = statusLine({ ...base, unreachable: true });
    expect(away.head).toBe("Box unreachable — retrying…");
    for (const s of [statusLine({ ...base, live: false }), away]) {
      expect(`${s.head} ${s.sub}`).not.toMatch(/Live/);
    }
  });

  it("counts tiles in while they load, and says when nobody has been to the End", () => {
    expect(statusLine({ ...base, tilesPending: 5 })).toEqual({
      tone: "spin",
      head: "Loading map tiles · 7 of 12",
      sub: "Live · 2 on · up to a minute behind",
    });
    expect(statusLine({ ...base, dim: "the_end", explored: false }).sub).toBe(
      "Nobody has been to the End — all of it is the survey",
    );
  });

  const slot = (o: Partial<MinecraftSlot>): MinecraftSlot =>
    ({
      id: "slot1",
      folder: "world",
      name: "World",
      exists: true,
      active: false,
      last_played: null,
      ...o,
    }) as MinecraftSlot;

  it("the world picker says which world is live", () => {
    expect(slotLine(slot({ active: true }), true)).toBe("loaded · being played now");
    expect(slotLine(slot({ active: true }), false)).toBe("loaded · server stopped");
    expect(slotLine(slot({ exists: false }), true)).toBe("empty · no world here");
    expect(slotLine(slot({ last_played: Date.UTC(2026, 9, 4, 12) / 1000 }), true)).toBe(
      "last played Oct 4",
    );
  });

  it("the Minecraft screen's row says who's on the map", () => {
    expect(entryLine("Castle Hill", ["BlockyFox", "Mira_P"])).toBe(
      "Castle Hill · BlockyFox and Mira_P on the map",
    );
    expect(entryLine("Castle Hill", [])).toBe("Castle Hill · nobody on right now");
  });
});

describe("scaleOfZoom", () => {
  it("reads a chat card's tile-route zoom as px per block, inside the map's range", () => {
    expect(scaleOfZoom(0)).toBe(1);
    expect(scaleOfZoom(-1)).toBe(2);
    expect(scaleOfZoom(4)).toBe(S_MIN);
    expect(scaleOfZoom(-3)).toBe(S_MAX);
    expect(scaleOfZoom(-9)).toBe(S_MAX);
  });
});

describe("the Tricorder line", () => {
  const at = { dim: "overworld" as const, x: 0.5, z: 0.5 };
  const target = (x: number, z: number, y: number | null = null) => ({
    dim: "overworld" as const,
    x,
    y,
    z,
    label: "Diamonds",
    set_at: 0,
  });

  it("says how far and which way, north being -z as in game", () => {
    expect(tricorderLine(at, target(0, -100))).toBe("Diamonds · 100 blocks north");
    expect(tricorderLine(at, target(100, 100))).toBe("Diamonds · 141 blocks south-east");
    expect(tricorderLine(at, target(-30, 0))).toBe("Diamonds · 30 blocks west");
    expect(tricorderLine(at, target(1, 0))).toBe("Diamonds · 1 block east");
  });

  it("adds the depth when it matters, and is plain about standing on it", () => {
    expect(tricorderLine(at, target(0, 40, -58))).toBe("Diamonds · 40 blocks south · y -58");
    expect(tricorderLine(at, target(0, 0, -58))).toBe("Diamonds · here");
  });

  it("names the dimension instead of a bearing across a portal", () => {
    expect(tricorderLine(at, { ...target(5, 5), dim: "nether" })).toBe("Diamonds · in the Nether");
    expect(tricorderLine({ ...at, dim: "the_end" }, target(5, 5))).toBe(
      "Diamonds · in the Overworld",
    );
  });
});

describe("the Tricorder's live scan in words (§M25)", () => {
  // The GUI gate's fixture: BlockyFox down a mine at y -52, a diamond at (41, -58, -79).
  const NOW = 10_000;
  const fox = (over: Partial<MinecraftMapPlayer> = {}): MinecraftMapPlayer => ({
    xuid: "1",
    gamertag: "BlockyFox",
    dim: "overworld",
    x: 32,
    y: -52,
    z: -70,
    at: NOW,
    online: true,
    target: { dim: "overworld", x: 300, y: null, z: 0, label: "Tree farm", set_at: 0 },
    scan: {
      label: "diamond",
      state: "found",
      found: { dim: "overworld", x: 41, y: -58, z: -79 },
      at: NOW - 40,
      set_at: NOW - 900,
    },
    ...over,
  });
  const none = { label: "diamond", state: "none" as const, found: null, at: NOW - 40, set_at: 0 };
  // The words keep numbers and units together with no-break spaces; compared as plain ones.
  const plain = <T>(v: T): T => JSON.parse(JSON.stringify(v).replace(/\u00a0/g, " "));

  it("says what the held Tricorder says: the distance across, then C's depth line", () => {
    expect(plain(scanWords(fox(), NOW))).toEqual({
      line: "Scanning: diamond · 13 blocks north-east · y -58",
      depth: "↓ 6 blocks below them · y -58",
      sub: "Within 16 blocks, y -64 to -36. Found 40 s ago. Their waypoint, Tree farm, comes back when the scan stops.",
    });
    // The same formatter as the waypoint's line: horizontal distance, never 3D.
    expect(scanWords(fox(), NOW)?.line).toBe(
      tricorderLine(fox(), {
        dim: "overworld",
        x: 41,
        y: -58,
        z: -79,
        label: "Scanning: diamond",
        set_at: 0,
      }),
    );
    const up = fox({ y: -60.5 });
    expect(plain(scanWords(up, NOW)?.depth)).toBe("↑ 3 blocks above them · y -58");
    expect(plain(scanWords(fox({ y: -58 }), NOW)?.depth)).toBe("Level with them · y -58");
  });

  it("none nearby says the same reach, and when it last looked", () => {
    expect(plain(scanWords(fox({ scan: none, target: null }), NOW))).toEqual({
      line: "Scanning: diamond · none nearby",
      depth: null,
      sub: "Nothing within 16 blocks, y -64 to -36. Last scan 40 s ago.",
    });
  });

  it("offline, the scan is kept and the card says it resumes; nothing is drawn", () => {
    const off = fox({ online: false });
    expect(plain(scanWords(off, NOW))).toEqual({
      line: "Scanning: diamond — resumes when BlockyFox is back on",
      depth: null,
      sub: "",
    });
    expect(scanPlate(off)).toBeNull();
  });

  it("before its first scan, and with no scan at all", () => {
    const set = { label: "oak log", state: "set" as const, found: null, at: null, set_at: NOW };
    expect(plain(scanWords(fox({ scan: set, target: null, y: 70 }), NOW))).toEqual({
      line: "Scanning: oak log",
      depth: null,
      sub: "Within 16 blocks, y 54 to 86. The first scan is on its way.",
    });
    expect(scanWords(fox({ scan: null }), NOW)).toBeNull();
  });

  it("one plate per scanner, their name first", () => {
    expect(scanPlate(fox())).toBe("BlockyFox · Scanning: diamond");
    expect(scanPlate(fox({ scan: none }))).toBe("BlockyFox · Scanning: diamond · none nearby");
    expect(scanPlate(fox({ scan: null }))).toBeNull();
  });

  it("the box's height is cut at the world's floor and ceiling, never shifted", () => {
    expect(scanYRange("overworld", -40)).toEqual([-56, -24]);
    expect(scanYRange("overworld", -52)).toEqual([-64, -36]);
    expect(scanYRange("overworld", 63.9)).toEqual([47, 79]);
    expect(scanYRange("overworld", 319)).toEqual([303, 319]);
    expect(scanYRange("nether", 120)).toEqual([104, 127]);
    expect(scanYRange("the_end", 5)).toEqual([0, 21]);
    expect(plain([agoWords(0, 40), agoWords(0, 61), agoWords(0, 7300)])).toEqual([
      "40 s ago",
      "1 min ago",
      "2 h ago",
    ]);
  });
});

describe("the trail and what it explored keep up with the players", () => {
  const log = (now: number): MinecraftTimeline => ({
    slot: "slot2",
    world: "castle",
    from: 0,
    to: now,
    now,
    trails: [
      {
        xuid: "fox",
        gamertag: "BlockyFox",
        samples: 4,
        downsampled: false,
        segments: [
          {
            session: "a",
            dim: "overworld",
            points: [
              [10, 0, 64, 0],
              [20, 40, 64, 0],
            ],
          },
          { session: "a", dim: "nether", points: [[30, 5, 70, 5]] },
          { session: "a", dim: "overworld", points: [[40, 300, 64, -20]] },
        ],
      },
    ],
  });
  const at = (t: number) => ({ at: t }) as MinecraftMapPlayer;

  it("a disc is ahead of its trail when its sample is newer than the log the trail came from", () => {
    expect(trailBehind([at(44), at(52)], log(50))).toBe(true);
    expect(trailBehind([at(44), at(50)], log(50))).toBe(false);
    expect(trailBehind([at(60)], null)).toBe(false);
  });

  it("the trail's tail is its newest point, and the steps since one tail are in one dimension", () => {
    expect(trailTail(log(50))).toBe(40);
    expect(trailTail(null)).toBeNull();
    expect(trailTail({ ...log(50), trails: [] })).toBeNull();
    expect(trailSince(log(50), 15, "overworld")).toEqual([
      [40, 0],
      [300, -20],
    ]);
    expect(trailSince(log(50), 40, "overworld")).toEqual([]);
  });

  it("new steps mark the tiles at every zoom whose explored ground they could change", () => {
    const near = tilesNear([[10, 10]], 64);
    // Zoom 0 tiles are 256 blocks: 64 blocks round (10, 10) reaches the three west and north.
    for (const k of ["0|0|0", "0|-1|0", "0|0|-1", "0|-1|-1"]) expect(near.has(k)).toBe(true);
    expect(near.has("0|1|0")).toBe(false);
    expect(near.has("4|0|0") && near.has("4|-1|-1")).toBe(true);
    expect(near.has("4|1|0")).toBe(false);
    expect(tilesNear([], 64).size).toBe(0);
  });
});
