import { describe, expect, it, vi } from "vitest";
import type { BaseLayer, TileSpot } from "../minecraftMap";
import { type TileEntry, type Tokens, drawTiles } from "./minecraftMapDraw";

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
