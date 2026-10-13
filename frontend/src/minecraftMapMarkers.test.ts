import { describe, expect, it } from "vitest";
import type { MinecraftGate } from "./api/client";
import { mcMapMarkers } from "./minecraftMapFixtures";
import {
  NO_AVOID,
  beaconArea,
  beaconWords,
  blocked,
  gateCenter,
  gateWords,
  linesHere,
  listRows,
  overlapsOf,
  overlayLines,
  segHitsBox,
  sideSlot,
} from "./minecraftMapMarkers";

const M = mcMapMarkers("slot2");
const gate = (id: number) => M.gates.find((g) => g.id === id) as MinecraftGate;

describe("the beacons' protected squares", () => {
  it("are the 3 × 3 chunks round the beacon, to the block, at negative coordinates too", () => {
    expect(beaconArea({ x: 60, z: -40 })).toEqual({ x0: 32, z0: -64, x1: 80, z1: -16 });
    expect(beaconArea({ x: -1, z: -17 })).toEqual({ x0: -32, z0: -48, x1: 16, z1: 0 });
  });

  it("count the neighbours whose squares share ground, in the same dimension", () => {
    const [mira, fox, sky] = M.beacons;
    if (!mira || !fox || !sky) throw new Error("fixture");
    expect(overlapsOf(mira, M.beacons)).toBe(1);
    expect(overlapsOf(sky, M.beacons)).toBe(0);
    expect(overlapsOf(mira, [mira, { ...fox, dim: "nether" }])).toBe(0);
  });

  it("say who placed it, the square in real minus signs, and the overlap", () => {
    const [mira] = M.beacons;
    if (!mira) throw new Error("fixture");
    const w = beaconWords(mira, M.beacons);
    expect(w.sub).toMatch(/^placed by Mira_P, [A-Z][a-z]{2} \d+$/);
    expect(w.area).toBe("X 32 to 79, Z −64 to −17");
    expect(w.overlaps).toBe(" · overlaps 1 other beacon");
    expect(beaconWords({ ...mira, placed_by: "", placed_at: null }, []).sub).toBe(
      "placer not recorded",
    );
  });
});

describe("the gates", () => {
  it("are marked at the middle of their ring, whichever way it runs", () => {
    expect(gateCenter(gate(1))).toEqual([30, 14.5]);
    expect(gateCenter(gate(2))).toEqual([-69.5, 62]);
  });

  it("draw a line only when both ends are in the dimension shown", () => {
    expect(linesHere(gate(1), "overworld")).toBe(true);
    expect(linesHere(gate(1), "nether")).toBe(false);
    expect(linesHere(gate(2), "overworld")).toBe(false); // dormant
    expect(linesHere(gate(3), "overworld")).toBe(false); // into the Nether: a label instead
  });

  it("say where they lead, or that they're dormant and how to open them", () => {
    const open = gateWords(gate(1));
    expect(open.title).toBe("Wormhole gate 1");
    expect(open.sub).toMatch(/^BlockyFox's · planted \w+ \d+, aimed \w+ \d+$/);
    expect(open.lead).toBe("244 blocks north-west · X −160 · Z −140");
    expect(open.show).toBe("Show Tree farm");
    expect(open.dormant).toBeNull();
    const nether = gateWords(gate(3));
    expect(nether.lead).toBe("in the Nether · X −20 · Z 40");
    expect(nether.show).toBe("Show it in the Nether");
    const dormant = gateWords(gate(2));
    expect(dormant.lead).toBeNull();
    expect(dormant.dormant).toBe(
      "Not open yet. Mira_P can ask Dave to open it to one of their saved places, standing within 4 blocks.",
    );
  });
});

describe("the Layers tab", () => {
  it("counts what's in the world under each switch", () => {
    expect(overlayLines(M)).toEqual({
      beacons: "3 in this world · each with its 48 × 48 protected area",
      gates: "2 open · 1 dormant · a dotted line to where each leads",
    });
    expect(overlayLines(null).gates).toBe("none in this world yet");
  });

  it("lists every beacon, then every gate, the sky base's height and the Nether's", () => {
    const rows = listRows(M);
    expect(rows.map((r) => r.title)).toEqual([
      "No-Spawn Beacon · Mira_P",
      "No-Spawn Beacon · BlockyFox",
      "No-Spawn Beacon · BlockyFox",
      "Gate 1 · BlockyFox",
      "Gate 2 · Mira_P",
      "Gate 3 · BlockyFox",
    ]);
    expect(rows[2]?.sub).toContain("up at Y 210");
    expect(rows[3]?.sub).toBe("open, to Tree farm · X 30 · Z 14");
    expect(rows[4]?.sub).toMatch(/^dormant · /);
    expect(rows[5]?.sub).toMatch(/^open, to Nether base \(Nether\) · /);
    expect(listRows(null)).toEqual([]);
  });
});

describe("where a gate's label may go", () => {
  const bounds = { w: 390, top: 4, bottom: 600 };
  const at = { x: 200, y: 300, r: 12, own: "g:1" };

  it("goes right of its marker when it can", () => {
    expect(sideSlot(at, 60, 20, bounds, [], [], NO_AVOID)).toEqual({
      x: 216,
      y: 290,
      w: 60,
      h: 20,
    });
  });

  it("tries the left, then below, then above, and is dropped when nothing is clear", () => {
    // Another gate's line runs right through the right-hand slot.
    const line = { ax: 250, ay: 260, bx: 250, by: 340, own: "g:2" };
    const left = sideSlot(at, 60, 20, bounds, [], [], { rects: [], segs: [line] });
    expect(left?.x).toBe(124);
    // A player's disc on the left: below.
    const disc = { x: 140, y: 290, w: 24, h: 24, own: "p:1" };
    const below = sideSlot(at, 60, 20, bounds, [], [], { rects: [disc], segs: [line] });
    expect(below).toEqual({ x: 170, y: 316, w: 60, h: 20 });
    // A label already below: above.
    const placed = [{ x: 170, y: 316, w: 60, h: 20 }];
    expect(sideSlot(at, 60, 20, bounds, [], placed, { rects: [disc], segs: [line] })?.y).toBe(264);
    // A control above too: nowhere — dropped, never drawn over any of them.
    const keep = [{ x: 0, y: 250, w: 390, h: 30 }];
    expect(sideSlot(at, 60, 20, bounds, keep, placed, { rects: [disc], segs: [line] })).toBeNull();
  });

  it("may touch its own gate's line and pin, never another's", () => {
    const r = { x: 210, y: 290, w: 60, h: 20 };
    const own = { ax: 200, ay: 300, bx: 300, by: 300, own: "g:1" };
    expect(blocked(r, "g:1", [], [], { rects: [], segs: [own] })).toBe(false);
    expect(blocked(r, "g:2", [], [], { rects: [], segs: [own] })).toBe(true);
    expect(blocked(r, undefined, [], [], { rects: [{ ...r, own: "g:1" }], segs: [] })).toBe(true);
  });

  it("knows a line through a box from one beside it", () => {
    const box = { x: 10, y: 10, w: 20, h: 10 };
    expect(segHitsBox({ ax: 0, ay: 15, bx: 40, by: 15 }, box)).toBe(true);
    expect(segHitsBox({ ax: 0, ay: 0, bx: 40, by: 40 }, box)).toBe(true);
    expect(segHitsBox({ ax: 0, ay: 25, bx: 40, by: 25 }, box)).toBe(false);
    expect(segHitsBox({ ax: 0, ay: 0, bx: 5, by: 5 }, box)).toBe(false);
  });
});
