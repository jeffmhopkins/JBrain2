import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type MapIntent, setMapOpener } from "../../minecraftMapNav";
import { parseMinecraftMap } from "./minecraftMapCard";
import { ToolView } from "./registry";

// The GUI gate's chat fixture: BlockyFox's lava death in the Nether, beside the fortress.
// "Now" is pinned the next day, so the card's day words are fixed.
const NOW = new Date(2026, 9, 10, 14, 40).getTime();
const DIED = Math.round(new Date(2026, 9, 9, 17, 12).getTime() / 1000);

function deathCard(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    slot: "slot2",
    world: "Castle Hill",
    loaded: true,
    dim: "nether",
    center: { x: 41, z: -58 },
    zoom: -1,
    overlays: { trails: true, deaths: true, structures: true, players: false },
    focus: {
      kind: "death",
      label: "BlockyFox died here",
      x: 41,
      y: 33,
      z: -58,
      at: DIED,
      cause: "lava",
      killer: null,
    },
    overworld: { x: 328, z: -464 },
    trail: { player: "2535411", gamertag: "BlockyFox", from: DIED - 7200, to: DIED + 120 },
    ...over,
  };
}

function show(data: Record<string, unknown>) {
  return render(
    <ToolView payload={{ view: "minecraft_map", surface: "inline", data, refs: [] }} />,
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A 2D context that draws nothing but remembers its words, so the scale bar and labels
 *  can be read back. */
function fakeContext(texts: string[]): CanvasRenderingContext2D {
  const target: Record<string, unknown> = {
    measureText: (s: string) => ({ width: s.length * 6 }),
    fillText: (s: string) => texts.push(s),
  };
  return new Proxy(target, {
    get: (t, k: string) => (k in t ? t[k] : () => undefined),
    set: () => true,
  }) as unknown as CanvasRenderingContext2D;
}

describe("the minecraft_map chat card", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let tiles: string[];
  let texts: string[];
  let opened: MapIntent[];

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    tiles = [];
    texts = [];
    opened = [];
    setMapOpener((i) => opened.push(i));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal(
      "Image",
      class {
        decoding = "";
        onload: (() => void) | null = null;
        onerror: (() => void) | null = null;
        set src(v: string) {
          tiles.push(v);
        }
      },
    );
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      () => fakeContext(texts) as never,
    );
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://box").pathname;
      if (path === "/api/minecraft/map/players") {
        return json({ slot: "slot2", world: "Castle Hill", live: true, players: [] });
      }
      if (path === "/api/minecraft/map/structures") {
        return json({
          slot: "slot2",
          dimension: "nether",
          structures: [{ structure: "fortress", x: 120, z: -90 }],
          swept_at: 1,
          stale: false,
          sweep: { state: "idle" },
        });
      }
      if (path === "/api/minecraft/timeline") {
        return json({ from: DIED - 7200, to: DIED + 120, trails: [], events: [] });
      }
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    setMapOpener(null);
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const asked = () => fetchMock.mock.calls.map(([u]) => String(u));

  it("shows the death, its Overworld side, the legend and the scale bar", async () => {
    show(deathCard());
    expect(screen.getByText("Castle Hill · Nether")).toBeInTheDocument();
    expect(screen.getByText("BlockyFox burned in lava")).toBeInTheDocument();
    expect(screen.getByText(/^Yesterday, 5:12/)).toBeInTheDocument();
    expect(screen.getByText("X 328 · Z -464")).toBeInTheDocument();
    expect(screen.getByText("Explored")).toBeInTheDocument();
    expect(screen.getByText("Satellite survey")).toBeInTheDocument();
    expect(screen.getByText("Death")).toBeInTheDocument();
    expect(screen.getByText("Structure")).toBeInTheDocument();
    // Honest status: nothing claimed until the box says, then live and how far behind.
    expect(screen.getByText("Loading the map…")).toBeInTheDocument();
    expect(await screen.findByText("Live · 0 on")).toBeInTheDocument();
    // The canvas draws the scale bar (north up) and the focus label.
    await waitFor(() => expect(texts).toContain("N↑"));
    expect(texts.some((t) => /blocks?$/.test(t) || / block$/.test(t))).toBe(true);
    expect(texts).toContain("BlockyFox died here");
  });

  it("builds every tile request itself and never follows a URL in the payload", async () => {
    show(
      deathCard({
        tile_url: "https://evil.example/{z}/{x}.png",
        url: "https://evil.example/map",
        src: "data:image/png;base64,AAAA",
        world: "Castle Hill",
      }),
    );
    await waitFor(() => expect(tiles.length).toBeGreaterThan(0));
    for (const t of tiles) {
      expect(t).toMatch(
        /^\/api\/minecraft\/map\/tile\/nether\/0\/-?\d+\/-?\d+\.png\?slot=slot2&layer=(surface|biomes)$/,
      );
    }
    await waitFor(() =>
      expect(asked().some((u) => u.includes("/api/minecraft/timeline"))).toBe(true),
    );
    for (const u of [...asked(), ...tiles]) {
      expect(u).not.toContain("evil");
      expect(u.startsWith("/api/minecraft/")).toBe(true);
    }
    // The trail and deaths are this player's own, over the payload's window.
    const tl = new URL(asked().find((u) => u.includes("/timeline")) ?? "", "http://box");
    expect(tl.searchParams.get("players")).toBe("2535411");
    expect(tl.searchParams.get("from")).toBe(String(DIED - 7200));
  });

  it("a tap opens the full map on the card's spot; a drag or a scroll does not", async () => {
    show(deathCard());
    const map = screen.getByRole("img", { name: /^Map of Castle Hill · Nether/ });
    // One finger dragging is the chat's scroll: the browser cancels, or it simply moved.
    fireEvent.pointerDown(map, { pointerId: 1, clientX: 10, clientY: 10, pointerType: "touch" });
    fireEvent.pointerCancel(map, { pointerId: 1, clientX: 10, clientY: 60 });
    fireEvent.pointerDown(map, { pointerId: 2, clientX: 10, clientY: 10, pointerType: "touch" });
    fireEvent.pointerMove(map, { pointerId: 2, clientX: 40, clientY: 10, pointerType: "touch" });
    fireEvent.pointerUp(map, { pointerId: 2, clientX: 40, clientY: 10 });
    expect(opened).toEqual([]);
    fireEvent.pointerDown(map, { pointerId: 3, clientX: 300, clientY: 200 });
    fireEvent.pointerUp(map, { pointerId: 3, clientX: 300, clientY: 200 });
    expect(opened).toEqual([{ slot: "slot2", dim: "nether", x: 41, z: -58, zoom: -1, mark: true }]);
    fireEvent.click(screen.getByRole("button", { name: "Open the full map" }));
    expect(opened).toHaveLength(2);
  });

  it("two fingers pinch the card, and the full map opens at the new zoom", async () => {
    show(deathCard());
    const map = screen.getByRole("img", { name: /^Map of/ });
    fireEvent.pointerDown(map, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerDown(map, { pointerId: 2, clientX: 140, clientY: 100 });
    fireEvent.pointerMove(map, { pointerId: 2, clientX: 420, clientY: 100 });
    fireEvent.pointerUp(map, { pointerId: 2, clientX: 420, clientY: 100 });
    fireEvent.pointerUp(map, { pointerId: 1, clientX: 100, clientY: 100 });
    expect(opened).toEqual([]); // a pinch is never a tap
    fireEvent.click(screen.getByRole("button", { name: "Open the full map" }));
    expect(opened[0]?.zoom).toBe(-3);
  });

  it("copies X Z as two numbers, and the overlay chips switch", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    show(deathCard());
    fireEvent.click(screen.getByRole("button", { name: "Copy X Z" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("41 -58"));
    expect(await screen.findByText("Copied X Z: 41 -58")).toBeInTheDocument();
    const chip = screen.getByRole("button", { name: "Structures" });
    expect(chip).toHaveAttribute("aria-pressed", "true");
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Players" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("a malformed payload draws nothing it shouldn't", () => {
    const { container } = show({ slot: "../../etc", dim: "<img src=x onerror=alert(1)>" });
    expect(screen.getByText("This map can't be shown.")).toBeInTheDocument();
    expect(container.querySelector("img, iframe, script, canvas")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("parseMinecraftMap", () => {
  it("clamps every slot and reads nothing else", () => {
    const card = parseMinecraftMap({
      slot: "slot3",
      dim: "aether",
      world: `  ${"W".repeat(90)}  `,
      center: { x: 9e12, z: "far" },
      zoom: 77,
      overlays: { trails: "yes", players: true, iframe: true },
      focus: { kind: "treasure", x: 1, z: 2 },
      trail: { player: "", gamertag: "x" },
      html: "<b>hi</b>",
    });
    expect(card).toEqual({
      slot: "slot3",
      world: "W".repeat(60),
      loaded: false,
      dim: "overworld",
      x: 30_000_000,
      z: 0,
      zoom: 4,
      overlays: { trails: false, deaths: false, structures: false, players: true },
      focus: null,
      trail: null,
    });
  });

  it("defaults the overlays and centres on the focus", () => {
    const card = parseMinecraftMap({
      slot: "slot1",
      focus: { kind: "spot", label: 42, x: 10.6, z: -3.2, y: "high" },
    });
    expect(card?.overlays).toEqual({
      trails: true,
      deaths: true,
      structures: true,
      players: false,
    });
    expect(card?.focus).toMatchObject({ kind: "spot", label: "", x: 11, z: -3, y: null });
    expect([card?.x, card?.z, card?.zoom]).toEqual([11, -3, 1]);
    expect(parseMinecraftMap({ slot: "slot0" })).toBeNull();
    expect(parseMinecraftMap({})).toBeNull();
  });
});
